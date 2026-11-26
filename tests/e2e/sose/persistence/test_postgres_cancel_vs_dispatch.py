"""#494/#578: cancel/withdraw versus claim of the same attested work, on PostgreSQL.

#578 asks for "cancel/withdraw vs claim/ACK ordering conflicts with exact cause/source
IDs" and "independently trusted transition receipts". Two OS processes race on one
durable scheduled ``ship``:

- the **claim**: an attested ``Engine.dispatch_scheduled`` (transition, event,
  receipt, Command consumed, all in one transaction);
- the **withdraw**: ``DurableScheduler.cancel`` (work and Command deleted together).

The interleaving is forced. One process stops inside its transaction, after it took
the owner lock and read the work. The parent then sees the other process in
``pg_stat_activity`` waiting on that advisory lock before it lets the first commit.
Read on a fresh connection, the outcome must be exactly one of:

- **claimed:** shipped once, one transition and one receipt naming the ``ship``
  Command, ``verify_history`` passes, and ``cancel`` reported ``False``;
- **withdrawn:** still ``packed`` with no ship transition and no receipt, and
  ``dispatch_scheduled`` reported ``False``.

Never both, and never "cancel succeeded" next to a completed ship. A mutant cancel
that skips the owner lock produces exactly that ghost and is caught.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from uuid import uuid4

import psycopg
import pytest

from sose.core.transition_authority import ATTESTATION_EVENT
from sose.persistence.postgres import PostgresPersistence
from tests.e2e.sose.persistence.test_postgres_attested_uncertain_commit import (
    ORDER,
    ORDER_TYPE,
    ROOT,
    SHIP,
    _seed_packed_with_pending_ship,
    pending_ship,
)

DSN = os.environ.get("SOSE_TEST_POSTGRES_DSN")
pytestmark = pytest.mark.skipif(not DSN, reason="real PostgreSQL TCP required")

_WORKER = """
import json, sys, time
from pathlib import Path
from sose.core.durable import DurableScheduler
from sose.persistence.postgres import PostgresPersistence
from tests.e2e.sose.persistence.test_postgres_attested_uncertain_commit import (
    OWNER, engine, pending_ship,
)

dsn, namespace, mode, hold, mutant, markers = sys.argv[1:]
markers = Path(markers)
original = PostgresPersistence._apply_changes
held = [False]

def holding(self, before, after, dirty_records):
    # Inside the transaction, after the owner lock and the reads: wait here until
    # the parent saw the other process blocked (or gave up waiting).
    if hold == "1" and not held[0] and self.namespace == namespace and dirty_records:
        held[0] = True
        (markers / f"{mode}-holding").write_text("1")
        deadline = time.monotonic() + 30
        while not (markers / "release").exists():
            if time.monotonic() > deadline:
                raise TimeoutError("parent never released the held transaction")
            time.sleep(0.02)
    return original(self, before, after, dirty_records)

PostgresPersistence._apply_changes = holding
def wait(name):
    deadline = time.monotonic() + 30
    while not (markers / name).exists():
        if time.monotonic() > deadline:
            raise TimeoutError(f"no {name} marker")
        time.sleep(0.02)

with PostgresPersistence(dsn, namespace=namespace) as store:
    item = pending_ship(store)              # both sides see the same pending work
    (markers / f"{mode}-ready").write_text("1")
    wait(f"{mode}-go")
    (markers / f"{mode}-started").write_text("1")
    try:
        if mode == "dispatch":
            result = engine(store).dispatch_scheduled(item)
        else:
            scheduler = DurableScheduler(store, owner_id=OWNER)
            if mutant == "1":
                # MUTANT: a cancel whose transaction skips the per-owner lock.
                original_tx = store.transaction
                store.transaction = lambda **kw: original_tx(
                    **{k: v for k, v in kw.items() if k != "owner_id"})
            result = scheduler.cancel(item.work.work_id)
        print(json.dumps({"result": result}))
    except Exception as exc:
        print(json.dumps({"error": f"{type(exc).__name__}: {exc}"}))
"""


def _spawn(namespace, mode, *, hold, mutant, markers):
    return subprocess.Popen(
        [sys.executable, "-c", _WORKER, DSN, namespace, mode,
         "1" if hold else "0", "1" if mutant else "0", str(markers)],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )


def _wait_for(path, process, timeout=30.0):
    deadline = time.monotonic() + timeout
    while not path.exists():
        if process.poll() is not None:
            out, err = process.communicate()
            pytest.fail(f"worker exited before {path.name}: {out[-800:]} {err[-2000:]}")
        if time.monotonic() > deadline:
            pytest.fail(f"timed out waiting for {path.name}")
        time.sleep(0.02)


def _blocked_on_advisory_lock(timeout=8.0) -> bool:
    """True once some backend waits on an advisory lock (the second process)."""
    deadline = time.monotonic() + timeout
    with psycopg.connect(DSN, autocommit=True) as conn:
        while time.monotonic() < deadline:
            row = conn.execute(
                "SELECT count(*) FROM pg_stat_activity WHERE wait_event_type = 'Lock' "
                "AND wait_event = 'advisory' AND query LIKE '%pg_advisory_xact_lock%'",
            ).fetchone()
            if row[0]:
                return True
            time.sleep(0.05)
    return False


def _result(process):
    out, err = process.communicate(timeout=60)
    lines = [line for line in out.splitlines() if line.startswith("{")]
    assert lines, (out[-800:], err[-3000:])
    return json.loads(lines[-1])


def _race(namespace, tmp_path, *, holder, mutant=False):
    """``holder`` stops inside its transaction; the other side must wait for it."""
    other = "cancel" if holder == "dispatch" else "dispatch"
    markers = tmp_path / f"markers-{holder}-{int(mutant)}"
    markers.mkdir()
    first = _spawn(namespace, holder, hold=True, mutant=mutant, markers=markers)
    second = _spawn(namespace, other, hold=False, mutant=mutant, markers=markers)
    try:
        _wait_for(markers / f"{holder}-ready", first)
        _wait_for(markers / f"{other}-ready", second)
        (markers / f"{holder}-go").write_text("1")
        _wait_for(markers / f"{holder}-holding", first)
        (markers / f"{other}-go").write_text("1")
        _wait_for(markers / f"{other}-started", second)
        blocked = _blocked_on_advisory_lock(timeout=2.0 if mutant else 8.0)
        (markers / "release").write_text("1")
        results = {holder: _result(first), other: _result(second)}
    finally:
        for process in (first, second):
            if process is not None and process.poll() is None:
                process.kill()
                process.communicate(timeout=10)
    return blocked, results


def _outcome(namespace):
    """Read the durable outcome on a fresh direct connection."""
    with PostgresPersistence(DSN, namespace=namespace) as store:
        events = [e for e in store.events() if e.entity_id == ORDER]
        ships = [e for e in events if e.name == "entity.state_transition"
                 and e.payload["trigger"] == "ship"]
        receipts = [e for e in events if e.name == ATTESTATION_EVENT
                    and e.payload["trigger"] == "ship"]
        entity = store.entity(ORDER_TYPE, ORDER)
        return entity, ships, receipts, pending_ship(store), events


@pytest.mark.parametrize("holder", ["dispatch", "cancel"])
def test_cancel_and_claim_of_one_work_have_exactly_one_winner(tmp_path, holder):
    assert DSN is not None
    namespace = "cancel_race_" + uuid4().hex[:12]
    _seed_packed_with_pending_ship(namespace)
    blocked, results = _race(namespace, tmp_path, holder=holder)
    assert blocked, "the second process must wait on the owner's advisory lock"
    entity, ships, receipts, pending, events = _outcome(namespace)
    assert pending is None, "the work and its Command are gone either way"
    if holder == "dispatch":
        assert results == {"dispatch": {"result": True}, "cancel": {"result": False}}
        assert (entity.state, entity.version) == ("shipped", 4)
        assert len(ships) == len(receipts) == 1
        assert ships[0].causation_id == SHIP
        assert receipts[0].payload["command_id"] == SHIP
        assert receipts[0].causation_id == ships[0].event_id
        ROOT.verify_history(events=events, entity=entity)
    else:
        assert results == {"cancel": {"result": True}, "dispatch": {"result": False}}
        assert (entity.state, entity.version) == ("packed", 3)
        assert ships == [] and receipts == []
        ROOT.verify_history(events=events, entity=entity)


def test_a_cancel_without_the_owner_lock_is_caught_as_a_ghost(tmp_path):
    """RED mutant: without the owner lock both sides commit, and the oracle sees it."""
    assert DSN is not None
    namespace = "cancel_mutant_" + uuid4().hex[:12]
    _seed_packed_with_pending_ship(namespace)
    blocked, results = _race(namespace, tmp_path, holder="dispatch", mutant=True)
    assert not blocked
    entity, ships, receipts, _pending, _events = _outcome(namespace)
    # The withdraw was acknowledged although the claim shipped: a ghost cancel.
    assert results["cancel"] == {"result": True}
    assert results["dispatch"] == {"result": True}
    assert entity.state == "shipped" and len(receipts) == 1

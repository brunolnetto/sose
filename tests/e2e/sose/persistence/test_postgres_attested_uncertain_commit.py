"""#494/#545: an attested ship under injected rollback and a lost COMMIT reply.

#545 requires that "Command + StateChart execution + canonical event + receipt are
indivisible in real persistence; injected rollback and post-COMMIT ambiguity cannot
create ghost/duplicate authority". A separate OS worker dispatches a durable,
scheduled ``ship`` through an attested Engine on real PostgreSQL:

- **lost COMMIT reply:** the wire-level proxy used by #584 forwards the COMMIT,
  witnesses the backend's CommandComplete and drops it before the worker reads it.
  The worker fails with a transport error and cannot know the outcome;
- **injected rollback:** the worker raises inside the transaction after the
  transition, event and receipt were written, so PostgreSQL rolls them back.

The parent never trusts the worker's exit code. It reads the outcome on a fresh
direct connection and asserts all-or-nothing: either the entity, the transition, the
receipt and the consumed Command are all present (and verify), or none is. A retry
then completes exactly once; it never adds a second transition or receipt.
"""
from __future__ import annotations

import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest

from sose.core.clock import SimulationClock
from sose.core.context import SimulationContext
from sose.core.durable import DurableScheduledItem
from sose.core.engine import Engine
from sose.core.events import Command
from sose.core.randomness import RandomSource
from sose.core.scheduler import Scheduler
from sose.core.transition_authority import ATTESTATION_EVENT, TransitionAuthority
from sose.domain.registry import DomainRegistry, EntityType
from sose.examples.warehouse_fulfillment.entities import FulfillmentOrder
from sose.examples.warehouse_fulfillment.statecharts import FulfillmentOrderChart
from sose.persistence.postgres import PostgresPersistence
from tests.e2e.sose.persistence.test_postgres_infrastructure_chaos import _free_port
from tests.e2e.sose.persistence.test_postgres_pc6_uncertain_effect_commit import (
    _start_commit_reply_drop_proxy,
)
from tests.support.chaos_waits import wait_for_marker_exists

DSN = os.environ.get("SOSE_TEST_POSTGRES_DSN")
pytestmark = pytest.mark.skipif(not DSN, reason="real PostgreSQL TCP required")
OWNER = "warehouse:attested-uncertain-commit"
NOW = datetime(2026, 11, 25, 10, tzinfo=timezone.utc)
ROOT = TransitionAuthority("engine:uncertain-commit", "key:v1", b"0123456789abcdef" * 2)
ORDER = "order:uncertain"
ORDER_TYPE = FulfillmentOrder(id=ORDER).entity_type
SHIP = "command:ship"


def engine(store: PostgresPersistence) -> Engine:
    registry = DomainRegistry()
    registry.register(EntityType(ORDER_TYPE, FulfillmentOrderChart))
    context = SimulationContext(
        clock=SimulationClock(now=NOW, step=timedelta(seconds=1)),
        random=RandomSource(root_seed=29),
        scheduler=Scheduler(),
    )
    return Engine(persistence=store, owner_id=OWNER, registry=registry,
                  context=context, transition_authority=ROOT)


def pending_ship(store: PostgresPersistence) -> DurableScheduledItem | None:
    with store.transaction(owner_id=OWNER) as uow:
        work = [item for item in uow.scheduled_work() if item.command_id == SHIP]
        commands = {command.command_id: command for command in uow.commands()}
    if not work:
        return None
    return DurableScheduledItem(work[0], commands[SHIP])


def _command(name: str, command_id: str) -> Command:
    return Command(command_id=command_id, name=name, entity_type=ORDER_TYPE,
                   entity_id=ORDER, owner_id=OWNER, due_at=NOW, issued_at=NOW,
                   correlation_id="flow:uncertain-commit")


def _seed_packed_with_pending_ship(namespace: str) -> None:
    """An attested order at ``packed`` (version 3) and a durable scheduled ship."""
    with PostgresPersistence(DSN, namespace=namespace) as store:
        with store.transaction(owner_id=OWNER) as uow:
            uow.save_entity(FulfillmentOrder(id=ORDER, state="requested",
                                             created_at=NOW, updated_at=NOW))
        runtime = engine(store)
        for step in ("allocate", "start_pick", "pack"):
            runtime.dispatch(_command(step, f"command:{step}"))
        runtime.scheduler.schedule(_command("ship", SHIP))
        assert store.entity(ORDER_TYPE, ORDER).version == 3
        assert pending_ship(store) is not None


# The worker uses ONLY the proxied (or patched) connection. It arms the fault
# inside the ship transaction, after the real transition, event and receipt rows
# were written, so the fault cannot hit an earlier or unrelated transaction.
_WORKER = """
import sys
from pathlib import Path
from sose.core.transition_authority import ATTESTATION_EVENT
from sose.persistence.postgres import PostgresPersistence
from tests.e2e.sose.persistence.test_postgres_attested_uncertain_commit import (
    engine, pending_ship,
)

dsn, namespace, mode, arm = sys.argv[1:]
original = PostgresPersistence._apply_changes

def after_attested_ship_rows(self, before, after, dirty_records):
    result = original(self, before, after, dirty_records)
    receipts = [e for e in after.events if e.name == ATTESTATION_EVENT
                and e.payload.get("trigger") == "ship"]
    if self.namespace == namespace and receipts:
        if mode == "rollback":
            raise RuntimeError("injected failure after the attested ship rows")
        Path(arm).write_text("armed_after_attested_ship_rows")
    return result

PostgresPersistence._apply_changes = after_attested_ship_rows
with PostgresPersistence(dsn, namespace=namespace) as store:
    item = pending_ship(store)
    assert item is not None, "the scheduled ship must still be pending"
    engine(store).dispatch_scheduled(item)
raise AssertionError("the attested ship committed with no fault")
"""


def _run_worker(dsn: str, namespace: str, mode: str, arm) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-c", _WORKER, dsn, namespace, mode, str(arm)],
        capture_output=True, text=True, timeout=90,
    )


def _ship_evidence(store: PostgresPersistence):
    events = [e for e in store.events() if e.entity_id == ORDER]
    ships = [e for e in events if e.name == "entity.state_transition"
             and e.payload["trigger"] == "ship"]
    receipts = [e for e in events if e.name == ATTESTATION_EVENT
                and e.payload.get("trigger") == "ship"]
    return store.entity(ORDER_TYPE, ORDER), ships, receipts, pending_ship(store), events


def _assert_shipped_once(store: PostgresPersistence) -> None:
    entity, ships, receipts, pending, events = _ship_evidence(store)
    assert (entity.state, entity.version) == ("shipped", 4)
    assert len(ships) == len(receipts) == 1 and receipts[0].causation_id == ships[0].event_id
    assert pending is None, "the Command is consumed in the same commit"
    ROOT.verify_history(events=events, entity=entity)


def _assert_retry_is_a_no_op(store: PostgresPersistence, stale) -> None:
    """The stale callback and a second ship add no transition and no receipt."""
    before = _ship_evidence(store)
    runtime = engine(store)
    assert runtime.dispatch_scheduled(stale) is False
    with pytest.raises(Exception):                         # shipped has no ship transition
        runtime.dispatch(_command("ship", "command:ship-again"))
    assert _ship_evidence(store)[1:3] == before[1:3]
    _assert_shipped_once(store)


def test_a_lost_commit_reply_leaves_one_complete_attested_ship(tmp_path):
    assert DSN is not None
    namespace = "attested_lost_" + uuid4().hex[:12]
    _seed_packed_with_pending_ship(namespace)
    with PostgresPersistence(DSN, namespace=namespace) as store:
        stale = pending_ship(store)
    proxy = None
    try:
        proxy, proxied, arm, claimed, dropped = _start_commit_reply_drop_proxy(
            DSN, port=_free_port(), tmp_path=tmp_path,
        )
        worker = subprocess.Popen(
            [sys.executable, "-c", _WORKER, proxied, namespace, "lost-reply", str(arm)],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        wait_for_marker_exists(
            dropped, proxy, timeout=65,
            premature_exit_label="COMMIT proxy died before the attested ship",
            timeout_label="the attested ship COMMIT was never intercepted",
            termination_timeout=10,
        )
        assert dropped.read_text(encoding="utf-8") == "commit_complete_response_dropped"
        assert claimed.is_file() and not arm.exists()
        try:
            out, err = worker.communicate(timeout=30)
        except subprocess.TimeoutExpired:
            worker.kill()
            out, err = worker.communicate(timeout=10)
            pytest.fail("the worker never observed the severed COMMIT reply")
        assert worker.returncode != 0, ("an unacknowledged COMMIT was accepted", out, err)
        assert "psycopg" in err.lower() or "operationalerror" in err.lower(), err[-3000:]
    finally:
        if proxy is not None and proxy.poll() is None:
            proxy.kill()
            proxy.communicate(timeout=10)

    # Fresh direct connection: the COMMIT happened, so everything is there, once.
    with PostgresPersistence(DSN, namespace=namespace) as recovered:
        _assert_shipped_once(recovered)
        _assert_retry_is_a_no_op(recovered, stale)


def test_an_injected_rollback_leaves_no_ghost_authority(tmp_path):
    assert DSN is not None
    namespace = "attested_rollback_" + uuid4().hex[:12]
    _seed_packed_with_pending_ship(namespace)
    with PostgresPersistence(DSN, namespace=namespace) as store:
        receipts_before = sum(e.name == ATTESTATION_EVENT for e in store.events())
        stale = pending_ship(store)

    result = _run_worker(DSN, namespace, "rollback", tmp_path / "unused-arm")
    assert result.returncode != 0
    assert "injected failure after the attested ship rows" in result.stderr, result.stderr[-3000:]

    with PostgresPersistence(DSN, namespace=namespace) as recovered:
        entity, ships, receipts, pending, events = _ship_evidence(recovered)
        assert (entity.state, entity.version) == ("packed", 3)
        assert ships == [] and receipts == []
        assert sum(e.name == ATTESTATION_EVENT for e in events) == receipts_before
        assert pending is not None, "the Command survives the rollback"
        ROOT.verify_history(events=events, entity=entity)
        # The retry completes it exactly once; the next retry is a no-op.
        assert engine(recovered).dispatch_scheduled(pending) is True
        _assert_shipped_once(recovered)
        _assert_retry_is_a_no_op(recovered, stale)

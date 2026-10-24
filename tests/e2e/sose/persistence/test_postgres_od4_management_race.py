"""#464: actual PostgreSQL multi-process management intake race falsifier.

No actor reassignment, physical resource allocation or protected issuer is
asserted. This tests only durable single-winner StateChart intake semantics.
"""
from __future__ import annotations

import os
import time
from datetime import datetime, timedelta, timezone
from multiprocessing import get_context
from uuid import uuid4

import pytest

from sose.core.identity import deterministic_id
from sose.organizational.od4_intake import (
    WORK_KIND, IntakeProtocol, admit_work, intervene_work,
)
from sose.persistence.postgres import PostgresPersistence

DSN = os.environ.get("SOSE_TEST_POSTGRES_DSN")
pytestmark = pytest.mark.skipif(not DSN, reason="SOSE_TEST_POSTGRES_DSN required")
OWNER = "od4-pg-contested"
PEER = "od4-pg-independent"
T0 = datetime(2026, 10, 22, 12, tzinfo=timezone.utc)
PROTOCOL = IntakeProtocol(
    model_spec_hash="od4-pg-actor-intake-v1", config_epoch=1,
    cost_us=7, protocol_id="od4-pg-matched-management",
)


def _worker(dsn, namespace, result, owner_id, key, action, tag):
    try:
        with PostgresPersistence(dsn, namespace=namespace) as store:
            pid = store._connection.execute("SELECT pg_backend_pid()").fetchone()[0]
            result.put((tag, "ready", pid, ""))
            event = intervene_work(
                store, owner_id=owner_id, work_key=key, protocol=PROTOCOL,
                cause_id="observation:" + tag, action=action,
                at=T0 + timedelta(minutes=1),
            )
        result.put((tag, "committed", event.event_id, ""))
    except BaseException as exc:
        result.put((tag, "rejected", type(exc).__name__, str(exc)))


def test_pg_competing_accept_reject_has_exactly_one_terminal_transition():
    """Competing OS writers cannot both finish intake; peer is independent."""
    assert DSN is not None
    namespace = "od4_intake_" + uuid4().hex[:12]
    with PostgresPersistence(DSN, namespace=namespace) as store:
        contested = admit_work(
            store, owner_id=OWNER, work_key="case", protocol=PROTOCOL, at=T0,
        )
        peer = admit_work(
            store, owner_id=PEER, work_key="case", protocol=PROTOCOL, at=T0,
        )

    ctx = get_context("spawn")
    results = ctx.Queue()
    arms = (
        (OWNER, "case", "accept", "accept"),
        (OWNER, "case", "reject", "reject"),
        (PEER, "case", "accept", "peer"),
    )
    workers = [
        ctx.Process(
            target=_worker,
            args=(DSN, namespace, results, owner, key, action, tag),
        )
        for owner, key, action, tag in arms
    ]
    observations = []
    try:
        with PostgresPersistence(DSN, namespace=namespace) as locker:
            # Hold only the contested source. Independently owned work MUST
            # finish without acquiring an invented namespace-global mutex.
            with locker.business_resource_guard("od4-intake:" + contested):
                # Start both contenders first. They must reach the exact
                # contested advisory-lock wait BEFORE we let the peer run.
                # Otherwise a namespace-global lock could pass this test
                # merely because the peer happened to finish first.
                for worker in workers[:2]:
                    worker.start()
                readiness = [results.get(timeout=45) for _ in range(2)]
                assert {r[0] for r in readiness} == {"accept", "reject"}
                assert all(r[1] == "ready" for r in readiness), readiness
                contender_pids = tuple(r[2] for r in readiness)
                deadline = time.monotonic() + 45
                while time.monotonic() < deadline:
                    waiting = locker._connection.execute(
                        "SELECT pid FROM pg_stat_activity "
                        "WHERE pid = ANY(%s) AND wait_event_type = 'Lock' "
                        "AND wait_event = 'advisory'",
                        (list(contender_pids),),
                    ).fetchall()
                    if {r[0] for r in waiting} == set(contender_pids):
                        break
                    time.sleep(0.05)
                else:
                    raise AssertionError(
                        "contended OD-4 writers never simultaneously waited "
                        "on the held PostgreSQL advisory lock"
                    )
                workers[2].start()
                assert results.get(timeout=45)[0:2] == ("peer", "ready")
                observations.append(results.get(timeout=45))
                assert observations[0][:2] == ("peer", "committed"), observations
                with PostgresPersistence(DSN, namespace=namespace) as observer:
                    assert observer.entity(WORK_KIND, contested).state == "pending"
                    assert observer.entity(WORK_KIND, peer).state == "accepted"
        for worker in workers:
            worker.join(timeout=45)
            assert not worker.is_alive(), "OD-4 PostgreSQL intake worker hung"
            assert worker.exitcode == 0, f"OD-4 worker crashed: {worker.exitcode}"
        observations += [results.get(timeout=10) for _ in range(2)]
    finally:
        for worker in workers:
            if worker.is_alive():
                worker.kill()
                worker.join(timeout=10)
            if worker.pid is not None:
                try:
                    worker.close()
                except ValueError:
                    pass
        results.close()
        results.join_thread()

    by_tag = {row[0]: row[1:] for row in observations}
    assert by_tag["peer"][0] == "committed"
    winners = [tag for tag in ("accept", "reject")
               if by_tag[tag][0] == "committed"]
    assert len(winners) == 1, f"double terminal intake or no progress: {by_tag}"
    loser = "reject" if winners[0] == "accept" else "accept"
    assert by_tag[loser][0] == "rejected"
    assert by_tag[loser][1] == "ValueError"
    assert "terminal work" in by_tag[loser][2]
    expected_state = {"accept": "accepted", "reject": "rejected"}[winners[0]]

    with PostgresPersistence(DSN, namespace=namespace) as recovered:
        target = recovered.entity(WORK_KIND, contested)
        independent = recovered.entity(WORK_KIND, peer)
        assert target.state == expected_state
        assert independent.state == "accepted"
        assert target.attributes["paid_management_us"] == 7
        assert independent.attributes["paid_management_us"] == 7
        contested_source = tuple(e for e in recovered.events() if (
            e.name == "entity.state_transition" and e.entity_id == contested
        ))
        contested_decisions = tuple(e for e in recovered.events() if (
            e.name == "od4.intake_decided" and e.entity_id == contested
        ))
        peer_source = tuple(e for e in recovered.events() if (
            e.name == "entity.state_transition" and e.entity_id == peer
        ))
        assert len(contested_source) == len(contested_decisions) == len(peer_source) == 1
        assert contested_source[0].payload["trigger"] == winners[0]
        assert contested_decisions[0].payload["source_event_id"] == contested_source[0].event_id
        assert contested_decisions[0].payload["paid_management_us"] == 7
        assert recovered.command(deterministic_id(
            "od4-command-v1", OWNER, contested, "observation:" + loser,
        )) is None
        assert recovered.command(deterministic_id(
            "od4-command-v1", OWNER, contested, "observation:" + winners[0],
        )) is not None
        assert recovered.command(deterministic_id(
            "od4-command-v1", PEER, peer, "observation:peer",
        )) is not None

"""Real PostgreSQL / OS-process OD-2 actor ownership falsifiers (#462).

No actor availability or resource reservation is inferred from a StateChart
state alone. These tests target the already implemented durable actor UoW.
"""
from __future__ import annotations

import os
import signal
from datetime import datetime, timedelta, timezone
from multiprocessing import get_context
from threading import Event
from uuid import uuid4

import pytest

from sose.core.identity import deterministic_id
from sose.organizational.od2_actor import ACTOR_KIND, seed_actor, transition_actor
from sose.persistence.postgres import PostgresPersistence


DSN = os.environ.get("SOSE_TEST_POSTGRES_DSN")
pytestmark = pytest.mark.skipif(not DSN, reason="SOSE_TEST_POSTGRES_DSN required")
OWNER = "od2-pg-organization"
PEER_OWNER = "od2-pg-independent-organization"
MODEL = "od2-pg-actor-model-v1"
T0 = datetime(2026, 10, 17, 9, tzinfo=timezone.utc)


def _enroll(store, *, key, owner_id=OWNER):
    actor_id = seed_actor(
        store, owner_id=owner_id, actor_key=key, model_spec_hash=MODEL,
        config_epoch=1, at=T0,
    )
    transition_actor(
        store, owner_id=owner_id, actor_id=actor_id, action="activate",
        cause_id=f"activate:{key}", at=T0 + timedelta(hours=1),
        model_spec_hash=MODEL, config_epoch=1,
    )
    return actor_id


def _assign(store, actor_id, cause_id, work_id, *, owner_id=OWNER):
    return transition_actor(
        store, owner_id=owner_id, actor_id=actor_id,
        action="assign", cause_id=cause_id, work_id=work_id,
        at=T0 + timedelta(hours=2),
        model_spec_hash=MODEL, config_epoch=1,
    )


def _actor_assignment_worker(
    dsn, namespace, barrier, results, actor_id, tag, owner_id,
):
    try:
        with PostgresPersistence(dsn, namespace=namespace) as store:
            barrier.wait(timeout=30)
            _assign(
                store, actor_id, f"assign:{tag}", f"work:{tag}",
                owner_id=owner_id,
            )
        results.put((tag, "applied", "", ""))
    except BaseException as exc:
        results.put((tag, "rejected", type(exc).__name__, str(exc)))


def _post_commit_actor_worker(dsn, namespace, actor_id, ready):
    with PostgresPersistence(dsn, namespace=namespace) as store:
        actor = _assign(store, actor_id, "assign:death", "work:death")
        assert actor.state == "active"
        ready.set()
        # Leave the OS interpreter + real database session alive.
        # The test parent must kill this process, not close() it gracefully.
        Event().wait(timeout=60)


def _join_workers(workers, *, timeout=45):
    # Caller owns Process lifecycle/cleanup; don't close handles early:
    # it must still collect results and enforce exit status on error paths.
    for worker in workers:
        worker.join(timeout=timeout)
        assert not worker.is_alive(), "actor PG worker hung under concurrent admission"
        assert worker.exitcode == 0, (
            f"actor PG worker crashed with exit code {worker.exitcode}"
        )


def test_pg_competing_actor_claims_are_fenced_and_peer_is_independent():
    """Two actual PG writers race for one actor; a distinct actor also works."""
    assert DSN is not None
    namespace = "od2_actor_claim_" + uuid4().hex[:12]
    with PostgresPersistence(DSN, namespace=namespace) as store:
        contested = _enroll(store, key="contested")
        peer = _enroll(store, key="peer", owner_id=PEER_OWNER)

    ctx = get_context("spawn")
    barrier = ctx.Barrier(3)
    results = ctx.Queue()
    actors = (
        (contested, "claim-a", OWNER),
        (contested, "claim-b", OWNER),
        (peer, "peer", PEER_OWNER),
    )
    workers = [
        ctx.Process(
            target=_actor_assignment_worker,
            args=(DSN, namespace, barrier, results, actor_id, tag, owner),
        )
        for actor_id, tag, owner in actors
    ]
    first = None
    try:
        with PostgresPersistence(DSN, namespace=namespace) as locker:
            # Block only the contested actor. The separately owned peer
            # must complete and commit before the guard is released.
            with locker.business_resource_guard("od2-actor:" + contested):
                for worker in workers:
                    worker.start()
                first = results.get(timeout=30)
                assert first[:2] == ("peer", "applied"), (
                    "independent organization stalled behind actor-specific "
                    f"contention: {first}"
                )
                with PostgresPersistence(DSN, namespace=namespace) as observer:
                    assert observer.entity(ACTOR_KIND, contested).state == "eligible"
                    assert observer.entity(ACTOR_KIND, peer).state == "active"
        # Two claims resume after release. Only one may commit.
        _join_workers(workers)
        outcomes = {
            tag: (status, kind, message)
            for tag, status, kind, message in (
                first, *(results.get(timeout=5) for _ in range(2))
            )
        }
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

    assert outcomes["peer"] == ("applied", "", "")
    winners = [tag for tag in ("claim-a", "claim-b")
               if outcomes[tag][0] == "applied"]
    assert len(winners) == 1, f"actor double-assigned or no progress: {outcomes}"
    loser = next(tag for tag in ("claim-a", "claim-b") if tag not in winners)
    assert outcomes[loser][0] == "rejected"
    # Unrelated codec/persistence ValueError is not actor-fencing evidence.
    loser_kind, loser_message = outcomes[loser][1:]
    if loser_kind == "ValueError":
        assert "already assigned" in loser_message.lower(), outcomes
    else:
        assert loser_kind == "TransitionNotAllowed", outcomes
        assert "assign" in loser_message.lower(), outcomes

    with PostgresPersistence(DSN, namespace=namespace) as store:
        actor = store.entity(ACTOR_KIND, contested)
        independent = store.entity(ACTOR_KIND, peer)
        assert actor.state == independent.state == "active"
        assert actor.attributes["assigned_work_id"] == f"work:{winners[0]}"
        assert independent.attributes["assigned_work_id"] == "work:peer"
        assert store.command(
            deterministic_id("od2-actor-command", OWNER, contested, f"assign:{loser}")
        ) is None, "rejected claim left a durable orphan Command"
        assert actor.attributes["time_us"]["admitted"] == 3600 * 1000000
        assert actor.attributes["time_us"]["eligible"] == 3600 * 1000000
        for subject in (contested, peer):
            events = [e for e in store.events() if e.entity_id == subject]
            assert len(events) == len({e.event_id for e in events}) == 2
            assert {e.payload["trigger"] for e in events} == {"activate", "assign"}
            assert all(
                store.command(e.causation_id) is not None
                and store.command(e.causation_id).owner_id == (
                    OWNER if subject == contested else PEER_OWNER
                )
                for e in events
            )
        assert store.command(
            next(e.causation_id for e in store.events()
                 if e.entity_id == contested and e.payload["trigger"] == "assign")
        ).payload["external_cause_id"] == f"assign:{winners[0]}"


def test_pg_killed_actor_worker_replays_committed_cause_without_double_event():
    """SIGKILL after real PG COMMIT cannot erase or replay a paid transition."""
    assert DSN is not None
    namespace = "od2_actor_kill_" + uuid4().hex[:12]
    with PostgresPersistence(DSN, namespace=namespace) as store:
        actor_id = _enroll(store, key="killed-worker")

    ctx = get_context("spawn")
    ready = ctx.Event()
    worker = ctx.Process(
        target=_post_commit_actor_worker,
        args=(DSN, namespace, actor_id, ready),
    )
    try:
        worker.start()
        assert ready.wait(timeout=35), (
            f"actor transition was never committed by child: {worker.exitcode}"
        )
        assert worker.is_alive(), "actor worker exited before OS SIGKILL"
        worker.kill()
        worker.join(timeout=15)
        assert not worker.is_alive()
        if os.name == "posix":
            assert worker.exitcode == -signal.SIGKILL
        else:
            assert worker.exitcode not in (None, 0)
    finally:
        if worker.is_alive():
            worker.kill()
            worker.join(timeout=15)
        if worker.pid is not None:
            worker.close()

    with PostgresPersistence(DSN, namespace=namespace) as recovered:
        actor = recovered.entity(ACTOR_KIND, actor_id)
        assert actor is not None and actor.state == "active"
        assert actor.attributes["assigned_work_id"] == "work:death"
        before = (
            actor,
            tuple(e for e in recovered.events() if e.entity_id == actor_id),
            tuple(recovered.resource_reservations()),
        )
        assert len(before[1]) == 2
        assert not before[2], "actor StateChart does not claim physical capacity"
        replay = _assign(recovered, actor_id, "assign:death", "work:death")
        assert replay == actor
        after = (
            recovered.entity(ACTOR_KIND, actor_id),
            tuple(e for e in recovered.events() if e.entity_id == actor_id),
            tuple(recovered.resource_reservations()),
        )
        assert after == before
        completed = transition_actor(
            recovered, owner_id=OWNER, actor_id=actor_id,
            action="complete", cause_id="complete:death",
            at=T0 + timedelta(hours=3), work_id="work:death",
            model_spec_hash=MODEL, config_epoch=1,
        )
        assert completed.state == "eligible"
        assert completed.attributes["assigned_work_id"] is None
        assert len([e for e in recovered.events() if e.entity_id == actor_id]) == 3

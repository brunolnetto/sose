"""OD-2 #462: real PostgreSQL successor-fork and OS-worker-death falsifiers.

A predecessor may have exactly one replacement. Its identity guard must also
protect *different* proposed successor identities on independent connections,
without globally serializing another organization's actor statechart.
"""
from __future__ import annotations

import os
import signal
from datetime import timedelta
from multiprocessing import get_context
from threading import Event
from time import monotonic, sleep
from uuid import uuid4

import pytest

from sose.core.identity import deterministic_id
from sose.organizational.od2_actor import ACTOR_KIND, seed_actor, transition_actor
from sose.persistence.postgres import PostgresPersistence
from tests.e2e.sose.persistence.test_postgres_od2_actor_concurrency import (
    OWNER, PEER_OWNER, MODEL, T0, _enroll, _assign,
)

DSN = os.getenv("SOSE_TEST_POSTGRES_DSN")
pytestmark = pytest.mark.skipif(not DSN, reason="real PostgreSQL required")
REPLACE_AT = T0 + timedelta(hours=3)


def _namespace() -> str:
    return "od2_lineage_" + uuid4().hex[:12]


def _retired_actor(store) -> str:
    predecessor = _enroll(store, key="retired")
    exited = transition_actor(
        store, owner_id=OWNER, actor_id=predecessor,
        action="exit", cause_id="retire:retired",
        at=T0 + timedelta(hours=2), model_spec_hash=MODEL, config_epoch=1,
    )
    assert exited.state == "exited"
    return predecessor


def _replace_worker(dsn, namespace, predecessor, actor_key, barrier, reports):
    try:
        with PostgresPersistence(dsn, namespace=namespace) as store:
            # Parent checks BOTH backends are waiting for the *same* specific
            # predecessor SQL advisory lock (not merely past a Python barrier).
            pid = store._connection.execute("SELECT pg_backend_pid()").fetchone()[0]
            reports.put(("backend:" + actor_key, "connected", pid))
            barrier.wait(timeout=30)
            successor_id = seed_actor(
                store, owner_id=OWNER, actor_key=actor_key,
                model_spec_hash=MODEL, config_epoch=1,
                at=REPLACE_AT, replaces_actor_id=predecessor,
            )
        reports.put((actor_key, "accepted", successor_id))
    except BaseException as exc:
        reports.put((actor_key, type(exc).__name__, str(exc)))


def _peer_worker(dsn, namespace, peer, barrier, reports):
    try:
        with PostgresPersistence(dsn, namespace=namespace) as store:
            barrier.wait(timeout=30)
            actor = _assign(
                store, peer, "assign:unrelated", "work:unrelated",
                owner_id=PEER_OWNER,
            )
        reports.put(("independent", "accepted", actor.id))
    except BaseException as exc:
        reports.put(("independent", type(exc).__name__, str(exc)))


def _committed_replacement_worker(dsn, namespace, predecessor, ready):
    with PostgresPersistence(dsn, namespace=namespace) as store:
        successor = seed_actor(
            store, owner_id=OWNER, actor_key="killed-successor",
            model_spec_hash=MODEL, config_epoch=1,
            at=REPLACE_AT, replaces_actor_id=predecessor,
        )
        assert store.entity(ACTOR_KIND, predecessor).attributes[
            "replaced_by_actor_id"
        ] == successor
        # Signal only after COMMIT. Parent must terminate the real process.
        ready.set()
        Event().wait(timeout=60)


def test_pg_competing_successors_linearize_predecessor_claim_without_global_lock():
    assert DSN
    ns = _namespace()
    with PostgresPersistence(DSN, namespace=ns) as seed:
        predecessor = _retired_actor(seed)
        peer = _enroll(seed, key="independent", owner_id=PEER_OWNER)

    ctx = get_context("spawn")
    barrier, reports = ctx.Barrier(3), ctx.Queue()
    contenders = ("candidate-a", "candidate-b")
    procs = [
        ctx.Process(target=_replace_worker, args=(
            DSN, ns, predecessor, key, barrier, reports,
        ))
        for key in contenders
    ] + [
        ctx.Process(target=_peer_worker, args=(
            DSN, ns, peer, barrier, reports,
        ))
    ]
    outcomes = []
    try:
        with PostgresPersistence(DSN, namespace=ns) as locked:
            # Competing successor IDs cannot each lock themselves: all must
            # serialize on their common predecessor. The independent actor is
            # required to commit while that specific predecessor is locked.
            with locked.business_resource_guard("od2-actor:" + predecessor):
                for proc in procs:
                    proc.start()
                # Three reports: both contender SQL backend IDs plus the
                # independent peer commit. Their delivery order is arbitrary.
                entered = [reports.get(timeout=35) for _ in range(3)]
                pids = {
                    key.removeprefix("backend:"): detail
                    for key, status, detail in entered
                    if key.startswith("backend:") and status == "connected"
                }
                assert set(pids) == set(contenders), entered
                independent = next(
                    (item for item in entered if item[0] == "independent"),
                    None,
                )
                assert independent is not None and independent[1] == "accepted", entered

                with PostgresPersistence(DSN, namespace=ns) as observer:
                    # PostgreSQL pg_locks reports UNGRANTED requests against
                    # the precise 2-key predecessor lock. That falsifies an
                    # implementation which drops _guard and happens to commit
                    # a peer before either contender reaches its real lock.
                    lock_key = "od2-actor:" + predecessor
                    deadline = monotonic() + 25
                    waiting = set()
                    while monotonic() < deadline:
                        rows = observer._connection.execute(
                            """
                            SELECT pid FROM pg_locks
                            WHERE locktype='advisory'
                              AND NOT granted AND objsubid = 2
                              AND classid = hashtext(%s)::oid
                              AND objid = hashtext(%s)::oid
                            """,
                            (ns, lock_key),
                        ).fetchall()
                        waiting = {int(row[0]) for row in rows}
                        if waiting == set(pids.values()):
                            break
                        sleep(0.05)
                    assert waiting == set(pids.values()), (
                        "both contenders did not reach the identical "
                        f"predecessor advisory lock; pids={pids} waiting={waiting}"
                    )
                    assert observer.entity(ACTOR_KIND, peer).attributes[
                        "assigned_work_id"
                    ] == "work:unrelated"
                    assert observer.entity(ACTOR_KIND, predecessor).attributes[
                        "replaced_by_actor_id"
                    ] is None
        for proc in procs:
            proc.join(timeout=45)
            assert not proc.is_alive(), "PostgreSQL successor worker deadlocked"
            assert proc.exitcode == 0, proc.exitcode
        outcomes = [independent] + [
            reports.get(timeout=10) for _ in contenders
        ]
    finally:
        for proc in procs:
            if proc.is_alive():
                proc.kill()
                proc.join(timeout=15)
            if proc.pid is not None:
                proc.close()
        reports.close()
        reports.join_thread()

    successors = {key: (status, detail) for key, status, detail in outcomes
                  if key in contenders}
    accepted = [key for key, (status, _) in successors.items()
                if status == "accepted"]
    assert len(accepted) == 1, successors
    rejected = next(key for key in contenders if key not in accepted)
    assert successors[rejected][0] == "ValueError", successors
    assert "predecessor already replaced" in successors[rejected][1]
    winner_id = deterministic_id("od2-actor", OWNER, accepted[0])
    loser_id = deterministic_id("od2-actor", OWNER, rejected)
    with PostgresPersistence(DSN, namespace=ns) as reopened:
        prior = reopened.entity(ACTOR_KIND, predecessor)
        winner = reopened.entity(ACTOR_KIND, winner_id)
        assert prior.state == "exited"
        assert prior.attributes["replaced_by_actor_id"] == winner_id
        assert winner is not None and winner.state == "admitted"
        assert winner.attributes["replaces_actor_id"] == predecessor
        assert reopened.entity(ACTOR_KIND, loser_id) is None
        assert reopened.entity(ACTOR_KIND, peer).attributes[
            "assigned_work_id"
        ] == "work:unrelated"
        # No rejected successor command/event and no duplication of paid exit.
        events = [e for e in reopened.events() if e.entity_id == predecessor]
        assert len(events) == len({e.event_id for e in events}) == 2
        assert {e.payload["trigger"] for e in events} == {"activate", "exit"}
        assert not any(e.entity_id == loser_id for e in reopened.events())
        assert reopened.resource_reservations() == ()


def test_pg_killed_successor_worker_cannot_fork_committed_lineage():
    assert DSN
    ns = _namespace()
    with PostgresPersistence(DSN, namespace=ns) as seed:
        predecessor = _retired_actor(seed)

    ctx = get_context("spawn")
    ready = ctx.Event()
    proc = ctx.Process(
        target=_committed_replacement_worker,
        args=(DSN, ns, predecessor, ready),
    )
    try:
        proc.start()
        assert ready.wait(timeout=35), (
            f"replacement never committed: exit={proc.exitcode}"
        )
        assert proc.is_alive(), "worker terminated before parent SIGKILL"
        proc.kill()
        proc.join(timeout=15)
        assert not proc.is_alive()
        if os.name == "posix":
            assert proc.exitcode == -signal.SIGKILL
        else:
            assert proc.exitcode not in (None, 0)
    finally:
        if proc.is_alive():
            proc.kill()
            proc.join(timeout=15)
        if proc.pid is not None:
            proc.close()

    expected = deterministic_id("od2-actor", OWNER, "killed-successor")
    with PostgresPersistence(DSN, namespace=ns) as recovered:
        predecessor_state = recovered.entity(ACTOR_KIND, predecessor)
        successor_state = recovered.entity(ACTOR_KIND, expected)
        assert predecessor_state.attributes["replaced_by_actor_id"] == expected
        assert successor_state.attributes["replaces_actor_id"] == predecessor
        with recovered.transaction() as snapshot:
            commands_before = tuple(snapshot.commands())
        before = (
            predecessor_state, successor_state,
            tuple(recovered.events()), commands_before,
        )
        with pytest.raises(ValueError, match="already exists"):
            seed_actor(
                recovered, owner_id=OWNER, actor_key="killed-successor",
                model_spec_hash=MODEL, config_epoch=1,
                at=REPLACE_AT, replaces_actor_id=predecessor,
            )
        with pytest.raises(ValueError, match="predecessor already replaced"):
            seed_actor(
                recovered, owner_id=OWNER, actor_key="fork-after-death",
                model_spec_hash=MODEL, config_epoch=1,
                at=REPLACE_AT + timedelta(hours=1),
                replaces_actor_id=predecessor,
            )
        assert recovered.entity(ACTOR_KIND, deterministic_id(
            "od2-actor", OWNER, "fork-after-death",
        )) is None
        with recovered.transaction() as snapshot:
            commands_after = tuple(snapshot.commands())
        assert before == (
            recovered.entity(ACTOR_KIND, predecessor),
            recovered.entity(ACTOR_KIND, expected),
            tuple(recovered.events()), commands_after,
        )

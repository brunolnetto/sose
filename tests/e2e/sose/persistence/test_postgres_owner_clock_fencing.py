"""#504: deterministic PostgreSQL owner-clock fencing with independent connections.

Both workers must observe the same pre-tick position *before either may enter
its transaction*. The test detects lost updates, not stochastic throughput.
"""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from threading import Barrier
from uuid import uuid4
import os

import pytest

from sose.core.clock import SimulationClock
from sose.core.context import SimulationContext
from sose.core.engine import Engine
from sose.core.randomness import RandomSource
from sose.core.runtime import SimulationPosition
from sose.core.scheduler import Scheduler
from sose.domain.registry import DomainRegistry
from sose.persistence.postgres import PostgresPersistence

DSN = os.getenv("SOSE_TEST_POSTGRES_DSN")
pytestmark = pytest.mark.skipif(not DSN, reason="real PostgreSQL required")
ORIGIN = datetime(2026, 1, 1, tzinfo=timezone.utc)


def _position(tick):
    return SimulationPosition(
        logical_time=ORIGIN + timedelta(hours=tick),
        execution_sequence=0,
        committed_sequence=0,
        logical_tick=tick,
    )


def _run_worker(namespace, owner, barrier):
    with PostgresPersistence(DSN, namespace=namespace) as store:
        context = SimulationContext(
            clock=SimulationClock(now=ORIGIN, step=timedelta(hours=1), tick=0),
            random=RandomSource(root_seed=7),
            scheduler=Scheduler(),
        )
        engine = Engine(
            context=context,
            registry=DomainRegistry(),
            persistence=store,
            owner_id=owner,
        )
        original = store.simulation_position
        reads = [0]

        def first_read(*, owner_id):
            position = original(owner_id=owner_id)
            if not reads[0]:
                reads[0] += 1
                barrier.wait(timeout=15)
            return position

        store.simulation_position = first_read
        try:
            engine.advance_tick()
            return "committed"
        except RuntimeError as exc:
            if "stale owner simulation position" not in str(exc):
                raise
            return "stale"


def test_postgres_same_owner_forced_pretransaction_overlap_fences_stale_worker():
    assert DSN
    namespace = "pc6_owner_fence_" + uuid4().hex[:12]
    owner = "organization-one"
    with PostgresPersistence(DSN, namespace=namespace) as store:
        with store.transaction(owner_id=owner) as uow:
            uow.set_simulation_position(_position(0), owner_id=owner)

    barrier = Barrier(2)
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [
            pool.submit(_run_worker, namespace, owner, barrier)
            for _ in range(2)
        ]
        outcomes = sorted(f.result(timeout=35) for f in futures)

    assert outcomes == ["committed", "stale"]
    with PostgresPersistence(DSN, namespace=namespace) as store:
        assert store.simulation_position(owner_id=owner) == _position(1)
        assert store.committed_tick(owner_id=owner) == 0


def test_postgres_different_owners_forced_overlap_advance_independently():
    assert DSN
    namespace = "pc6_owner_independent_" + uuid4().hex[:12]
    owners = ("organization-one", "organization-two")
    with PostgresPersistence(DSN, namespace=namespace) as store:
        with store.transaction() as uow:
            for owner in owners:
                uow.set_simulation_position(_position(0), owner_id=owner)

    barrier = Barrier(2)
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [
            pool.submit(_run_worker, namespace, owner, barrier)
            for owner in owners
        ]
        assert [f.result(timeout=35) for f in futures] == [
            "committed", "committed",
        ]
    with PostgresPersistence(DSN, namespace=namespace) as store:
        for owner in owners:
            assert store.simulation_position(owner_id=owner) == _position(1)
            assert store.committed_tick(owner_id=owner) == 0


def test_postgres_same_owner_parallel_scheduling_allocates_distinct_local_sequence():
    assert DSN
    from sose.core.durable import DurableScheduler
    from sose.core.events import Command

    namespace = "pc6_owner_schedule_" + uuid4().hex[:12]
    owner = "organization-one"
    barrier = Barrier(2)

    def schedule(index):
        with PostgresPersistence(DSN, namespace=namespace) as store:
            scheduler = DurableScheduler(store, owner_id=owner)
            command = Command(
                command_id=f"owner-one-command-{index}",
                name="advance", entity_type="shipment", entity_id=str(index),
                due_at=ORIGIN + timedelta(hours=1),
                owner_id=owner,
            )
            barrier.wait(timeout=15)
            return scheduler.schedule(command)

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(schedule, index) for index in range(2)]
        works = [f.result(timeout=35) for f in futures]

    assert {work.sequence for work in works} == {1, 2}
    assert len({work.work_id for work in works}) == 2
    assert all(work.owner_id == owner for work in works)
    with PostgresPersistence(DSN, namespace=namespace) as store:
        assert len(store.scheduled_work()) == 2

"""#504: forced PostgreSQL overlap of same-owner fencing and foreign progress."""
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
from datetime import datetime, timedelta, timezone
from threading import Event
from uuid import uuid4
import os

import pytest

from sose.core.runtime import SimulationPosition
from sose.persistence.postgres import PostgresPersistence

DSN = os.environ.get("SOSE_TEST_POSTGRES_DSN")
pytestmark = pytest.mark.skipif(not DSN, reason="real PostgreSQL required")
ORIGIN = datetime(2026, 1, 1, tzinfo=timezone.utc)


def position(tick: int) -> SimulationPosition:
    return SimulationPosition(
        logical_time=ORIGIN + timedelta(hours=tick),
        logical_tick=tick,
        execution_sequence=tick,
        committed_sequence=tick,
    )


def test_pg_owner_fence_serializes_competing_writers_without_global_lock():
    """A's UoW holds its owner fence; B must read A's committed value.
    
    C is a different owner and must be able to commit while A remains inside
    an open transaction. This is a forced schedule, not probabilistic stress.
    """
    assert DSN is not None
    namespace = "pc6_owner_barrier_" + uuid4().hex[:12]
    a_inside = Event()
    release_a = Event()
    b_attempt = Event()

    def writer_a():
        with PostgresPersistence(DSN, namespace=namespace) as store:
            with store.transaction(owner_id="organization-a") as uow:
                assert uow.get_simulation_position(owner_id="organization-a") is None
                uow.set_simulation_position(position(1), owner_id="organization-a")
                a_inside.set()
                assert release_a.wait(timeout=15), "test did not release first owner writer"

    def writer_b():
        assert a_inside.wait(timeout=15)
        with PostgresPersistence(DSN, namespace=namespace) as store:
            b_attempt.set()
            with store.transaction(owner_id="organization-a") as uow:
                prior = uow.get_simulation_position(owner_id="organization-a")
                assert prior == position(1), "owner B observed stale same-owner pre-commit state"
                uow.set_simulation_position(position(2), owner_id="organization-a")

    def other_owner():
        assert b_attempt.wait(timeout=15)
        with PostgresPersistence(DSN, namespace=namespace) as store:
            with store.transaction(owner_id="organization-c") as uow:
                assert uow.get_simulation_position(owner_id="organization-c") is None
                uow.set_simulation_position(position(3), owner_id="organization-c")

    with ThreadPoolExecutor(max_workers=3) as pool:
        first = pool.submit(writer_a)
        assert a_inside.wait(timeout=15)
        second = pool.submit(writer_b)
        third = pool.submit(other_owner)
        try:
            # The different owner must progress while A's transaction is held.
            third.result(timeout=10)
        finally:
            release_a.set()
        first.result(timeout=15)
        second.result(timeout=15)

    with PostgresPersistence(DSN, namespace=namespace) as store:
        assert store.simulation_position(owner_id="organization-a") == position(2)
        assert store.simulation_position(owner_id="organization-c") == position(3)

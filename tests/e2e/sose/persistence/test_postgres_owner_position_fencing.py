"""#504: real PostgreSQL concurrent stale-update falsifier.

Two real independent connections enter the same persisted owner recovery boundary,
then attempt to publish the same next sequence. This is a safety check against a
lost-update masquerading as progress; PostgreSQL connection-local reruns cannot
replace an interleaving-fenced test.
"""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from threading import Barrier
from uuid import uuid4
import os

import pytest

from sose.core.runtime import SimulationPosition
from sose.persistence.postgres import PostgresPersistence


DSN = os.environ.get("SOSE_TEST_POSTGRES_DSN")
pytestmark = pytest.mark.skipif(not DSN, reason="requires PostgreSQL")


def test_same_owner_competing_positions_cannot_both_commit_one_sequence():
    assert DSN is not None
    namespace = "owner_fencing_" + uuid4().hex[:12]
    owner = "organization-a"
    origin = datetime(2026, 1, 1, tzinfo=timezone.utc)
    prior = SimulationPosition(origin, execution_sequence=0, committed_sequence=0)
    next_pos = SimulationPosition(
        origin + timedelta(hours=1), execution_sequence=1, committed_sequence=1,
        logical_tick=1,
    )
    with PostgresPersistence(DSN, namespace=namespace) as setup:
        with setup.transaction() as uow:
            uow.set_simulation_position(prior, owner_id=owner)

    reads_complete = Barrier(2)

    def worker(label):
        try:
            with PostgresPersistence(DSN, namespace=namespace) as persistence:
                with persistence.transaction() as uow:
                    observed = persistence.simulation_position(owner_id=owner)
                    assert observed == prior, (label, observed)
                    reads_complete.wait(timeout=15)
                    uow.set_simulation_position(next_pos, owner_id=owner)
            return "committed"
        except Exception as exc:
            return type(exc).__name__

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(worker, ("worker-1", "worker-2")))
    assert results.count("committed") <= 1, (
        "two same-owner workers advanced the identical authoritative "
        f"boundary without fencing: {results}"
    )
    with PostgresPersistence(DSN, namespace=namespace) as verify:
        assert verify.simulation_position(owner_id=owner) in (prior, next_pos)


def test_distinct_owners_advance_independently_in_same_postgres_namespace():
    assert DSN is not None
    namespace = "owner_independent_" + uuid4().hex[:12]
    origin = datetime(2026, 1, 1, tzinfo=timezone.utc)
    initial = SimulationPosition(origin, execution_sequence=0, committed_sequence=0)
    advanced = SimulationPosition(
        origin + timedelta(hours=1), execution_sequence=1, committed_sequence=1,
        logical_tick=1,
    )
    owners = ("organization-a", "organization-b")
    with PostgresPersistence(DSN, namespace=namespace) as setup:
        with setup.transaction() as uow:
            for owner in owners:
                uow.set_simulation_position(initial, owner_id=owner)

    read_barrier = Barrier(2)

    def advance(owner):
        with PostgresPersistence(DSN, namespace=namespace) as store:
            with store.transaction() as uow:
                assert store.simulation_position(owner_id=owner) == initial
                read_barrier.wait(timeout=15)
                uow.set_simulation_position(advanced, owner_id=owner)

    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(advance, owners))

    with PostgresPersistence(DSN, namespace=namespace) as verify:
        for owner in owners:
            assert verify.simulation_position(owner_id=owner) == advanced

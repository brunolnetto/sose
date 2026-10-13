"""RED: independent same-type StateCharts must not inherit a shared PG clock.

Deliberately fails on the legacy namespace-global SimulationPosition.
Two real PostgreSQL connections force A to read its recovery boundary first;
B advances a distinct owner by five logical minutes; A then rebuilds at its
own unchanged boundary. No sleep, retry, or global mutex hides the interleaving.

This is an initial counterexample, not complete #504 certification. It does
not yet test fenced owner-position writes, persisted owner bindings, or replay.
"""
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Event
from uuid import uuid4
import os

import pytest

from sose.backends.simpy import SimPyBackend
from sose.core.runtime import SimulationPosition
from sose.examples.logistics.simulation import (
    ORIGIN, build_runtime, seed_reference,
)
from sose.persistence.postgres import PostgresPersistence


DSN = os.environ.get("SOSE_TEST_POSTGRES_DSN")
pytestmark = pytest.mark.skipif(not DSN, reason="SOSE_TEST_POSTGRES_DSN required")


def test_two_same_type_owners_keep_independent_pg_recovery_boundaries():
    assert DSN is not None
    namespace = "pc6_owner_time_" + uuid4().hex[:12]
    with PostgresPersistence(DSN, namespace=namespace) as initial:
        shipment_a = seed_reference(initial, instance_key="organization-a")
        shipment_b = seed_reference(initial, instance_key="organization-b")
        assert shipment_a.shipment_id != shipment_b.shipment_id
        assert len([
            item for item in initial.scheduled_work()
            if initial.command(item.command_id) is not None
            and initial.command(item.command_id).entity_type == "shipment"
        ]) == 2
        with initial.transaction() as uow:
            uow.set_simulation_position(
                SimulationPosition(
                    logical_time=ORIGIN, execution_sequence=0,
                    committed_sequence=0, logical_tick=0,
                )
            )

    first_owner_sampled = Event()
    second_owner_committed = Event()

    def restore_organization_a():
        with PostgresPersistence(DSN, namespace=namespace) as store:
            sampled = store.simulation_position()
            assert sampled is not None and sampled.logical_time == ORIGIN
            _, engine = build_runtime(store, now=sampled.logical_time)
            backend = SimPyBackend(origin=sampled.logical_time)
            first_owner_sampled.set()
            assert second_owner_committed.wait(15), "other owner did not commit"
            # The existing RuntimeRebuilder sees B's *shared* new position
            # rather than the position that A sampled for its own job.
            # Expected future contract: scoped owner A rebuild succeeds and
            # attaches exactly A's still-pending shipment callback.
            return engine.rebuild_backend(backend)

    def advance_organization_b():
        assert first_owner_sampled.wait(15), "first owner did not sample"
        with PostgresPersistence(DSN, namespace=namespace) as store:
            _, engine = build_runtime(
                store, now=ORIGIN, step=timedelta(minutes=5),
            )
            engine.advance_tick()
            advanced = store.simulation_position()
            assert advanced is not None
            assert advanced.logical_time == ORIGIN + timedelta(minutes=5)
        second_owner_committed.set()
        return advanced

    with ThreadPoolExecutor(max_workers=2) as pool:
        a = pool.submit(restore_organization_a)
        b = pool.submit(advance_organization_b)
        committed = b.result(timeout=20)
        assert committed.logical_time == ORIGIN + timedelta(minutes=5)
        attached_a = a.result(timeout=20)

    # This assertion will also remain RED after the clock is isolated unless
    # callbacks of the same StateChart type are bound to the correct owner.
    assert attached_a == 1

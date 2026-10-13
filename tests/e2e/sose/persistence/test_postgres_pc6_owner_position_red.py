"""RED counterexample: the legacy namespace clock aliases same-type flows.

Two PostgreSQL connections force A to sample a shared SimulationPosition,
B to advance the namespace clock, then A to rebuild at the old timestamp.

IMPORTANT: `instance_key` isolates entity IDs, NOT organization/job ownership.
The legacy runtime persists neither owner binding nor owner-scoped clock here.
This test demonstrates the *global-clock hazard*, not two autonomous
organizations; its desired owner-specific assertion cannot pass until the
fixture is rebuilt using future durable organization/job bindings (#504).
No timing luck, process-wide lock, or retry hides the interleaving.
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


def test_unscoped_same_type_pg_clock_alias_is_red_for_future_owner_binding():
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
            # The legacy runtime sees the namespace clock B just advanced.
            # This initial RED intentionally has NO owner binding yet.
            # Complete the fixture by persisting explicit org/job ownership,
            # then route A's backend through its scoped cursor and callbacks.
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

    # This future assertion is NOT a presently-valid legacy expectation:
    # the unscoped registry sees two shipment callbacks. It becomes an
    # executable conformance gate only with persisted A/B owner bindings.
    assert attached_a == 1

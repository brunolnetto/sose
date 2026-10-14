"""PC6: independent real PostgreSQL organizations survive a forced recovery interleaving.

This ports the two-connection falsifier from #505 to the breaking owner-only
contract. The two shipments share one StateChart type and one SQL namespace,
not a logical clock. No sleeps, retry-to-green, or global lock are involved.
"""
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Event
from uuid import uuid4
import os

import pytest

from sose.backends.simpy import SimPyBackend
from sose.core.durable import RuntimeRebuilder
from sose.core.runtime import SimulationPosition
from sose.examples.logistics.simulation import ORIGIN, build_runtime, seed_reference
from sose.persistence.postgres import PostgresPersistence


DSN = os.environ.get("SOSE_TEST_POSTGRES_DSN")
pytestmark = pytest.mark.skipif(not DSN, reason="real PostgreSQL required")


def _initial_position():
    return SimulationPosition(
        logical_time=ORIGIN,
        execution_sequence=0,
        committed_sequence=0,
        logical_tick=0,
    )


def test_same_type_organization_a_rebuilds_after_b_advances_its_own_clock():
    assert DSN is not None
    namespace = "pc6_owner_interleaving_" + uuid4().hex[:12]
    owners = ("org-a", "org-b")

    with PostgresPersistence(DSN, namespace=namespace) as initial:
        a = seed_reference(
            initial, owner_id=owners[0], instance_key="organization-a",
        )
        b = seed_reference(
            initial, owner_id=owners[1], instance_key="organization-b",
        )
        assert a.shipment_id != b.shipment_id
        shipment_work = [
            work for work in initial.scheduled_work()
            if (command := initial.command(work.command_id)) is not None
            and command.entity_type == "shipment"
        ]
        assert len(shipment_work) == 2
        assert {work.owner_id for work in shipment_work} == set(owners)
        with initial.transaction() as uow:
            for owner in owners:
                uow.set_simulation_position(_initial_position(), owner_id=owner)

    a_sampled = Event()
    b_committed = Event()

    def rebuild_a():
        with PostgresPersistence(DSN, namespace=namespace) as store:
            sampled = store.simulation_position(owner_id=owners[0])
            assert sampled == _initial_position()
            backend = SimPyBackend(origin=sampled.logical_time)
            a_sampled.set()
            assert b_committed.wait(timeout=15), "organization B did not commit"
            attached = RuntimeRebuilder(
                store, owner_id=owners[0],
            ).rebuild(backend, on_due=lambda _item: None)
            assert store.simulation_position(owner_id=owners[0]) == sampled
            return attached

    def advance_b():
        assert a_sampled.wait(timeout=15), "organization A did not sample"
        try:
            with PostgresPersistence(DSN, namespace=namespace) as store:
                _, engine = build_runtime(
                    store, owner_id=owners[1], now=ORIGIN,
                    step=timedelta(minutes=5),
                )
                engine.advance_tick()
                position = store.simulation_position(owner_id=owners[1])
                assert position is not None
                assert position.logical_time == ORIGIN + timedelta(minutes=5)
                return position
        finally:
            b_committed.set()

    with ThreadPoolExecutor(max_workers=2) as pool:
        future_a = pool.submit(rebuild_a)
        future_b = pool.submit(advance_b)
        advanced_b = future_b.result(timeout=30)
        attached_a = future_a.result(timeout=30)

    assert attached_a == 1, "owner A must not reattach owner B's shipment"
    with PostgresPersistence(DSN, namespace=namespace) as recovered:
        assert recovered.simulation_position(owner_id=owners[0]) == _initial_position()
        assert recovered.simulation_position(owner_id=owners[1]) == advanced_b
        assert len([
            work for work in recovered.scheduled_work()
            if work.owner_id == owners[0]
        ]) == 1
        assert len([
            work for work in recovered.scheduled_work()
            if work.owner_id == owners[1]
        ]) == 1

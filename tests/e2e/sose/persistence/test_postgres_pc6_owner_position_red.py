"""RED counterexample: the legacy namespace clock aliases same-type flows.

Two PostgreSQL connections force A to sample a shared SimulationPosition,
B to advance the namespace clock, then A to rebuild at the old timestamp.

The fixture now persists explicit organization/job bindings on BOTH
shipment ScheduledWork items. Owner A uses a scoped RuntimeRebuilder, which
must eventually see only its own callback. Owner B still advances via the
legacy unscoped Engine, exposing the missing owner-scoped clock update:
binding callbacks alone cannot isolate the global SimulationPosition.
This is not yet a complete owner-scoped Engine or fencing conformance test.
No timing luck, process-wide lock, or retry hides the interleaving.
"""
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Event
from uuid import uuid4
import os

import pytest

from sose.backends.simpy import SimPyBackend
from sose.core.runtime import ScheduledWorkOwnerBinding, SimulationPosition
from sose.core.durable import RuntimeRebuilder
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
        work = initial.scheduled_work()
        assert len([
            item for item in work
            if initial.command(item.command_id) is not None
            and initial.command(item.command_id).entity_type == "shipment"
        ]) == 2
        with initial.transaction() as uow:
            for item in work:
                command = initial.command(item.command_id)
                assert command is not None
                org, job = (
                    ("org-a", "job-a") if command.entity_id == shipment_a.shipment_id
                    else ("org-b", "job-b")
                )
                uow.bind_scheduled_work_owner(
                    ScheduledWorkOwnerBinding(item.work_id, org, job)
                )
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
            backend = SimPyBackend(origin=sampled.logical_time)
            first_owner_sampled.set()
            assert second_owner_committed.wait(15), "other owner did not commit"
            # Owner bindings now exist and this rebuilder is scoped,
            # but the single namespace-global SimulationPosition was still
            # overwritten by B's legacy Engine.advance_tick().
            return RuntimeRebuilder(
                store, owner_key=("org-a", "job-a"),
            ).rebuild(backend, on_due=lambda item: None)

    def advance_organization_b():
        assert first_owner_sampled.wait(15), "first owner did not sample"
        with PostgresPersistence(DSN, namespace=namespace) as store:
            _, engine = build_runtime(
                store, now=ORIGIN, step=timedelta(minutes=5),
            )
            # This legacy *unscoped* Engine still writes a shared clock,
            # even though both scheduled callbacks now have real durable
            # organization/job bindings. The next GREEN phase must route
            # this operation through an owner-scoped engine transaction.
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

    # Scoped work-owner bindings already exclude B's same-type callback.
    # This is reached only after the global-clock RED is repaired.
    assert attached_a == 1

"""The same StateChart type must never infer ScheduledWork ownership by type.

First RED for the durable organization/job binding prerequisite of #504.
No owner-scoped SimulationPosition or worker fencing is certified here.
"""
import pytest

from sose.core.runtime import ScheduledWorkOwnerBinding
from sose.core.durable import RuntimeRebuilder
from sose.backends.simpy import SimPyBackend
from sose.examples.logistics.simulation import ORIGIN, seed_reference
from sose.persistence.memory import MemoryPersistence


def test_scoped_same_type_work_requires_complete_durable_owner_bindings():
    store = MemoryPersistence()
    left = seed_reference(store, instance_key="left")
    right = seed_reference(store, instance_key="right")
    assert left.shipment_id != right.shipment_id
    assert len(store.scheduled_work()) == 2

    # An unbound legacy namespace cannot be silently activated as scoped.
    with pytest.raises(RuntimeError, match="unowned scheduled work"):
        store.scheduled_work_for_owner("organization-left", "job-left")

    for work in store.scheduled_work():
        command = store.command(work.command_id)
        assert command is not None and command.entity_type == "shipment"
        org, job = (
            ("organization-left", "job-left") if command.entity_id == left.shipment_id
            else ("organization-right", "job-right")
        )
        with store.transaction() as uow:
            uow.bind_scheduled_work_owner(
                ScheduledWorkOwnerBinding(
                    work_id=work.work_id, organization_id=org, job_id=job,
                )
            )

    left_work = store.scheduled_work_for_owner("organization-left", "job-left")
    right_work = store.scheduled_work_for_owner("organization-right", "job-right")
    assert len(left_work) == len(right_work) == 1
    assert left_work[0].work_id != right_work[0].work_id
    assert store.command(left_work[0].command_id).entity_id == left.shipment_id
    assert store.command(right_work[0].command_id).entity_id == right.shipment_id

    # The owner filter must be applied before scheduling backend callbacks.
    left_backend = SimPyBackend(origin=ORIGIN)
    attached = RuntimeRebuilder(
        store, owner_key=("organization-left", "job-left"),
    ).rebuild(left_backend, on_due=lambda item: None)
    assert attached == 1


def test_scheduled_owner_binding_is_immutable_and_durable():
    store = MemoryPersistence()
    seed_reference(store)
    work = store.scheduled_work()[0]
    binding = ScheduledWorkOwnerBinding(
        work_id=work.work_id, organization_id="org-1", job_id="job-1",
    )
    with store.transaction() as uow:
        uow.bind_scheduled_work_owner(binding)
    assert store.scheduled_work_owner(work.work_id) == binding
    with store.transaction() as uow:
        uow.bind_scheduled_work_owner(binding)  # idempotent same identity
    with pytest.raises(ValueError, match="already bound"):
        with store.transaction() as uow:
            uow.bind_scheduled_work_owner(
                ScheduledWorkOwnerBinding(work.work_id, "org-2", "job-2")
            )
    with store.transaction() as uow:
        uow.delete_scheduled_work(work.work_id)
    assert store.scheduled_work_owner(work.work_id) is None


@pytest.mark.parametrize("organization_id,job_id", [("", "job"), ("org", ""), (" ", "job")])
def test_scheduled_work_owner_refuses_empty_identity(organization_id, job_id):
    with pytest.raises(ValueError):
        ScheduledWorkOwnerBinding("work-1", organization_id, job_id)

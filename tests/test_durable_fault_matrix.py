from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from sose.backends.base import ResourceLease
from sose.core.clock import SimulationClock
from sose.core.context import SimulationContext
from sose.core.engine import Engine
from sose.core.randomness import RandomSource
from sose.core.resources import DurableResourceManager
from sose.core.runtime import ResourceDefinition, ResourceReservation
from sose.core.scheduler import Scheduler
from sose.domain.registry import DomainRegistry, EntityType
from sose.examples.mro.entities import WorkOrder
from sose.examples.mro.statecharts import WorkOrderChart
from sose.persistence.memory import MemoryPersistence


ORIGIN = datetime(2026, 1, 1, 8, tzinfo=timezone.utc)


def build_engine(store: MemoryPersistence, *, now: datetime = ORIGIN):
    context = SimulationContext(
        clock=SimulationClock(now=now, step=timedelta(hours=1)),
        random=RandomSource(42),
        scheduler=Scheduler(),
    )
    registry = DomainRegistry()
    registry.register(EntityType("work_order", WorkOrderChart))
    engine = Engine(
        context=context,
        registry=registry,
        persistence=store,
    )
    return context, engine


class RecordingBackend:
    def __init__(self, *, now=ORIGIN):
        self.now = now
        self.scheduled = []
        self.capacity = {}
        self.active = {}
        self.queues = {}
        self._sequence = 0

    def schedule_at(self, at, callback, *, priority=100, key=None):
        self.scheduled.append((at, priority, key, callback))

    def create_resource(self, name, *, capacity=1):
        self.capacity[name] = capacity
        self.active[name] = {}
        self.queues[name] = []

    def request_resource(self, name, *, request_id, on_acquired, priority=100):
        self._sequence += 1
        item = (priority, self._sequence, request_id, on_acquired)
        if len(self.active[name]) < self.capacity[name]:
            self._grant(name, item)
        else:
            self.queues[name].append(item)
            self.queues[name].sort(key=lambda value: (value[0], value[1]))

    def release_resource(self, lease):
        lease_id = lease.lease_id if isinstance(lease, ResourceLease) else lease
        for name, active in self.active.items():
            request_id = next(
                (
                    request_id
                    for request_id, current in active.items()
                    if current.lease_id == lease_id
                ),
                None,
            )
            if request_id is None:
                continue
            active.pop(request_id)
            if self.queues[name]:
                self._grant(name, self.queues[name].pop(0))
            return
        raise KeyError(lease_id)

    def _grant(self, name, item):
        _, _, request_id, callback = item
        lease = ResourceLease(
            lease_id=f"lease-{name}-{request_id}",
            request_id=request_id,
            resource_name=name,
            acquired_at=self.now,
        )
        self.active[name][request_id] = lease
        callback(lease)


def test_fault_matrix_schedule_persisted_before_backend_enqueue_recovers():
    store = MemoryPersistence()
    context, engine = build_engine(store)
    work_order = context.entities.create(
        WorkOrder,
        key=("fault-matrix", "schedule"),
        state="planned",
    )
    with store.transaction() as uow:
        uow.save_entity(work_order)

    class FailingScheduleBackend(RecordingBackend):
        def schedule_at(self, at, callback, *, priority=100, key=None):
            raise RuntimeError("backend enqueue failed")

    engine.rebuild_backend(FailingScheduleBackend())

    command = context.commands.create(
        "release",
        target=work_order,
        due_at=ORIGIN + timedelta(hours=1),
        key=("fault-matrix", "schedule", work_order.id),
    )

    with pytest.raises(RuntimeError, match="backend enqueue failed"):
        context.schedules.at(command.due_at, command=command)

    assert store.command(command.command_id) == command
    assert len(store.scheduled_work()) == 1

    _, recovered_engine = build_engine(store)
    recovered_backend = RecordingBackend()
    assert recovered_engine.rebuild_backend(recovered_backend) == 1
    assert len(recovered_backend.scheduled) == 1


def test_fault_matrix_demand_persisted_before_backend_request_recovers():
    store = MemoryPersistence()
    with store.transaction() as uow:
        uow.save_resource_definition(ResourceDefinition("bay", 1))

    class FailingRequestBackend:
        def request_resource(self, name, *, request_id, on_acquired, priority=100):
            raise RuntimeError("backend request failed")

    manager = DurableResourceManager(store)

    with pytest.raises(RuntimeError, match="backend request failed"):
        manager.request(
            FailingRequestBackend(),
            resource_name="bay",
            request_id="wo-1",
            requested_at=ORIGIN,
            priority=10,
        )

    assert [d.request_id for d in store.resource_demands()] == ["wo-1"]
    assert store.resource_reservations() == ()

    recovered_backend = RecordingBackend()
    recovered = DurableResourceManager(store)
    recovered.rebuild_backend(recovered_backend)

    assert store.resource_demands() == ()
    assert [r.request_id for r in store.resource_reservations()] == ["wo-1"]
    assert list(recovered_backend.active["bay"]) == ["wo-1"]


def test_fault_matrix_backend_grant_before_durable_commit_recovers():
    store = MemoryPersistence()
    with store.transaction() as uow:
        uow.save_resource_definition(ResourceDefinition("bay", 1))

    class FailingGrantManager(DurableResourceManager):
        def commit_grant(self, *, request_id, acquired_at):
            raise RuntimeError("grant commit failed")

    first_backend = RecordingBackend()
    first_backend.create_resource("bay", capacity=1)
    manager = FailingGrantManager(store)

    with pytest.raises(RuntimeError, match="grant commit failed"):
        manager.request(
            first_backend,
            resource_name="bay",
            request_id="wo-1",
            requested_at=ORIGIN,
            priority=10,
        )

    assert [d.request_id for d in store.resource_demands()] == ["wo-1"]
    assert store.resource_reservations() == ()

    recovered_backend = RecordingBackend()
    DurableResourceManager(store).rebuild_backend(recovered_backend)

    assert store.resource_demands() == ()
    assert [r.request_id for r in store.resource_reservations()] == ["wo-1"]


def test_fault_matrix_release_intent_before_backend_release_recovers():
    store = MemoryPersistence()
    with store.transaction() as uow:
        uow.save_resource_definition(ResourceDefinition("bay", 1))
        uow.save_resource_reservation(
            ResourceReservation(
                "res-holder",
                "holder",
                "bay",
                ORIGIN,
                sequence=1,
            )
        )

    class FailingReleaseBackend(RecordingBackend):
        def release_resource(self, lease):
            raise RuntimeError("backend release failed")

    first_backend = FailingReleaseBackend()
    manager = DurableResourceManager(store)
    manager.rebuild_backend(first_backend)

    with pytest.raises(RuntimeError, match="backend release failed"):
        manager.release(first_backend, "res-holder")

    assert [r.reservation_id for r in store.resource_reservations()] == ["res-holder"]
    assert len(store.resource_release_intents()) == 1

    recovered_backend = RecordingBackend()
    DurableResourceManager(store).rebuild_backend(recovered_backend)

    assert store.resource_release_intents() == ()
    assert store.resource_reservations() == ()
    assert recovered_backend.active["bay"] == {}

from __future__ import annotations

import heapq
from datetime import datetime, timedelta, timezone

from sose.backends.base import ResourceLease
from sose.core.clock import SimulationClock
from sose.core.context import SimulationContext
from sose.core.engine import Engine
from sose.core.randomness import RandomSource
from sose.core.runtime import ResourceDefinition
from sose.core.scheduler import Scheduler
from sose.domain.registry import DomainRegistry, EntityType
from sose.examples.mro.entities import WorkOrder
from sose.examples.mro.statecharts import WorkOrderChart
from sose.persistence.memory import MemoryPersistence
from sose.scenarios import AttributeEffect, Scenario, TickTrigger


ORIGIN = datetime(2026, 1, 1, 8, tzinfo=timezone.utc)


class IntegratedBackend:
    def __init__(self, *, now: datetime):
        self._now = now
        self._sequence = 0
        self._scheduled = []
        self.capacity = {}
        self.active = {}
        self.queues = {}
        self.grant_order = []

    @property
    def now(self):
        return self._now

    def schedule_at(self, at, callback, *, priority=100, key=None):
        self._sequence += 1
        heapq.heappush(self._scheduled, (at, priority, self._sequence, callback))
        return None

    def run_until(self, at):
        while self._scheduled and self._scheduled[0][0] <= at:
            due_at, _, _, callback = heapq.heappop(self._scheduled)
            self._now = due_at
            callback()
        self._now = at

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
        return None

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
            acquired_at=self._now,
        )
        self.active[name][request_id] = lease
        self.grant_order.append(request_id)
        callback(lease)


def build(store, *, now, tick=0):
    context = SimulationContext(
        clock=SimulationClock(now=now, step=timedelta(hours=1), tick=tick),
        random=RandomSource(42),
        scheduler=Scheduler(),
    )
    registry = DomainRegistry()
    registry.register(EntityType("work_order", WorkOrderChart))
    scenario = Scenario(
        name="maintenance_window",
        trigger=TickTrigger(every=1),
        duration=timedelta(hours=8),
        effects=(AttributeEffect("maintenance.window", True),),
    )
    engine = Engine(
        context=context,
        registry=registry,
        persistence=store,
        scenarios=(scenario,),
    )
    return context, engine


def seed(store):
    context, engine = build(store, now=ORIGIN)
    work_order = context.entities.create(
        WorkOrder,
        key=("full-runtime-equivalence", 1),
        state="planned",
    )
    with store.transaction() as uow:
        uow.save_entity(work_order)
        uow.save_resource_definition(ResourceDefinition("bay", 1))

    for offset, name in (
        (1, "release"),
        (2, "start"),
        (3, "complete"),
        (4, "close"),
    ):
        command = context.commands.create(
            name,
            target=work_order,
            due_at=ORIGIN + timedelta(hours=offset),
            key=("full-runtime-equivalence", work_order.id, name),
        )
        context.schedules.at(command.due_at, command=command)
    return work_order.id


def durable_snapshot(store, work_order_id):
    return {
        "entity": store.entity("work_order", work_order_id),
        "events": store.events(),
        "work": store.scheduled_work(),
        "position": store.simulation_position(),
        "scenario": store.scenario_state(),
        "definitions": store.resource_definitions(),
        "demands": store.resource_demands(),
        "reservations": store.resource_reservations(),
        "release_intents": store.resource_release_intents(),
    }


def run_continuous():
    store = MemoryPersistence()
    work_order_id = seed(store)
    context, engine = build(store, now=ORIGIN)
    backend = IntegratedBackend(now=ORIGIN)

    engine.rebuild_backend(backend)
    engine.resources.request(
        backend,
        resource_name="bay",
        request_id="holder",
        requested_at=ORIGIN,
        priority=100,
    )
    engine.resources.request(
        backend,
        resource_name="bay",
        request_id="urgent",
        requested_at=ORIGIN,
        priority=1,
    )
    engine.resources.request(
        backend,
        resource_name="bay",
        request_id="normal",
        requested_at=ORIGIN,
        priority=100,
    )

    holder = next(r for r in store.resource_reservations() if r.request_id == "holder")

    engine.advance_tick()
    backend.run_until(ORIGIN + timedelta(hours=2))
    engine.resources.release(backend, holder.reservation_id)
    backend.run_until(ORIGIN + timedelta(hours=4))

    return store, work_order_id, backend.grant_order


def run_with_two_restarts():
    store = MemoryPersistence()
    work_order_id = seed(store)

    context1, engine1 = build(store, now=ORIGIN)
    backend1 = IntegratedBackend(now=ORIGIN)
    engine1.rebuild_backend(backend1)
    engine1.resources.request(
        backend1,
        resource_name="bay",
        request_id="holder",
        requested_at=ORIGIN,
        priority=100,
    )
    engine1.resources.request(
        backend1,
        resource_name="bay",
        request_id="urgent",
        requested_at=ORIGIN,
        priority=1,
    )
    engine1.resources.request(
        backend1,
        resource_name="bay",
        request_id="normal",
        requested_at=ORIGIN,
        priority=100,
    )

    engine1.advance_tick()
    backend1.run_until(ORIGIN + timedelta(hours=2))

    position1 = store.simulation_position()
    context2, engine2 = build(store, now=position1.logical_time)
    backend2 = IntegratedBackend(now=position1.logical_time)
    engine2.rebuild_backend(backend2)

    holder = next(r for r in store.resource_reservations() if r.request_id == "holder")
    engine2.resources.release(backend2, holder.reservation_id)
    backend2.run_until(ORIGIN + timedelta(hours=3))

    position2 = store.simulation_position()
    context3, engine3 = build(store, now=position2.logical_time)
    backend3 = IntegratedBackend(now=position2.logical_time)
    engine3.rebuild_backend(backend3)
    backend3.run_until(ORIGIN + timedelta(hours=4))

    return store, work_order_id, backend1.grant_order + backend2.grant_order + backend3.grant_order


def test_full_runtime_is_equivalent_across_multiple_restarts():
    continuous_store, continuous_id, continuous_grants = run_continuous()
    restarted_store, restarted_id, restarted_grants = run_with_two_restarts()

    assert durable_snapshot(restarted_store, restarted_id) == durable_snapshot(
        continuous_store,
        continuous_id,
    )
    assert restarted_store.entity("work_order", restarted_id).state == "closed"
    assert restarted_store.scheduled_work() == ()
    assert restarted_store.resource_release_intents() == ()
    assert [r.request_id for r in restarted_store.resource_reservations()] == ["urgent"]
    assert [d.request_id for d in restarted_store.resource_demands()] == ["normal"]
    assert restarted_store.scenario_state() == continuous_store.scenario_state()

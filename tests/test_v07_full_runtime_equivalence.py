from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sose.backends.simpy import SimPyBackend
from sose.core.clock import SimulationClock
from sose.core.context import SimulationContext
from sose.core.engine import Engine
from sose.core.randomness import RandomSource
from sose.core.runtime import (
    ContainerDefinition,
    PreemptiveResourceDefinition,
    ResourceDefinition,
    StoreDefinition,
)
from sose.core.scheduler import Scheduler
from sose.domain.registry import DomainRegistry, EntityType
from sose.examples.mro.entities import WorkOrder
from sose.examples.mro.statecharts import WorkOrderChart
from sose.persistence.memory import MemoryPersistence

ORIGIN = datetime(2026, 1, 1, 8, tzinfo=timezone.utc)


def build(store: MemoryPersistence, *, now: datetime) -> tuple[SimulationContext, Engine]:
    context = SimulationContext(
        clock=SimulationClock(now=now, step=timedelta(hours=1)),
        random=RandomSource(42),
        scheduler=Scheduler(),
    )
    registry = DomainRegistry()
    registry.register(EntityType("work_order", WorkOrderChart))
    return context, Engine(context=context, registry=registry, persistence=store)


def seed(store: MemoryPersistence) -> str:
    context, engine = build(store, now=ORIGIN)
    work_order = context.entities.create(
        WorkOrder,
        key=("v0.7-full-runtime", 1),
        state="planned",
    )
    with store.transaction() as uow:
        uow.save_entity(work_order)
        uow.save_resource_definition(ResourceDefinition("bay", capacity=1))

    engine.preemptive_resources.define(
        PreemptiveResourceDefinition("crew", capacity=1)
    )
    engine.stores.define(StoreDefinition("inbox", kind="fifo", capacity=2))
    engine.containers.define(
        ContainerDefinition("fuel", capacity=100.0, initial=20.0)
    )

    for offset, trigger in (
        (1, "release"),
        (2, "start"),
        (3, "complete"),
        (4, "close"),
    ):
        command = context.commands.create(
            trigger,
            target=work_order,
            due_at=ORIGIN + timedelta(hours=offset),
            key=("v0.7-full-runtime", work_order.id, trigger),
        )
        context.schedules.at(command.due_at, command=command)

    return work_order.id


def start_operational_state(
    store: MemoryPersistence,
    work_order_id: str,
) -> tuple[Engine, SimPyBackend]:
    context, engine = build(store, now=ORIGIN)
    backend = SimPyBackend(origin=ORIGIN)
    assert engine.rebuild_backend(backend) == 4

    engine.resources.request(
        backend,
        resource_name="bay",
        request_id="bay-holder",
        requested_at=ORIGIN,
    )
    backend.run_until(ORIGIN)

    engine.preemptive_resources.request(
        backend,
        resource_name="crew",
        request_id="planned-crew",
        requested_at=ORIGIN,
        priority=100,
    )
    backend.run_until(ORIGIN)
    engine.preemptive_resources.request(
        backend,
        resource_name="crew",
        request_id="emergency-crew",
        requested_at=ORIGIN,
        priority=1,
        preempt=True,
    )
    backend.run_until(ORIGIN)

    engine.stores.put(
        backend,
        store_name="inbox",
        item_id="part-1",
        value={"sku": "bearing"},
        requested_at=ORIGIN,
    )
    backend.run_until(ORIGIN)
    engine.stores.get(
        backend,
        store_name="inbox",
        request_id="consume-part-1",
        requested_at=ORIGIN,
    )
    backend.run_until(ORIGIN)

    engine.containers.get(
        backend,
        container_name="fuel",
        request_id="consume-fuel",
        amount=10.0,
        requested_at=ORIGIN,
    )
    backend.run_until(ORIGIN)

    assert store.entity("work_order", work_order_id).state == "planned"
    return engine, backend


def durable_snapshot(store: MemoryPersistence, work_order_id: str) -> dict[str, object]:
    return {
        "entity": store.entity("work_order", work_order_id),
        "events": store.events(),
        "scheduled_work": store.scheduled_work(),
        "position": store.simulation_position(),
        "scenario_state": store.scenario_state(),
        "resource_definitions": store.resource_definitions(),
        "resource_demands": store.resource_demands(),
        "resource_reservations": store.resource_reservations(),
        "resource_release_intents": store.resource_release_intents(),
        "preemptive_definitions": store.preemptive_resource_definitions(),
        "preemptive_demands": store.preemptive_resource_demands(),
        "preemptive_reservations": store.preemptive_resource_reservations(),
        "preemptive_release_intents": store.preemptive_resource_release_intents(),
        "preemption_results": store.resource_preemption_results(),
        "store_definitions": store.store_definitions(),
        "store_items": store.store_items(),
        "store_put_intents": store.store_put_intents(),
        "store_get_requests": store.store_get_requests(),
        "store_get_results": store.store_get_results(),
        "container_definitions": store.container_definitions(),
        "container_states": store.container_states(),
        "container_intents": store.container_operation_intents(),
        "container_results": store.container_operation_results(),
    }


def run_continuous() -> tuple[MemoryPersistence, str]:
    store = MemoryPersistence()
    work_order_id = seed(store)
    _, backend = start_operational_state(store, work_order_id)
    backend.run_until(ORIGIN + timedelta(hours=4))
    return store, work_order_id


def run_with_three_restarts() -> tuple[MemoryPersistence, str]:
    store = MemoryPersistence()
    work_order_id = seed(store)
    _, backend1 = start_operational_state(store, work_order_id)

    backend1.run_until(ORIGIN + timedelta(hours=1))
    for boundary in (2, 3, 4):
        position = store.simulation_position()
        assert position is not None
        context, engine = build(store, now=position.logical_time)
        backend = SimPyBackend(origin=position.logical_time)
        engine.rebuild_backend(backend)
        assert context.clock.now == position.logical_time
        backend.run_until(ORIGIN + timedelta(hours=boundary))

    return store, work_order_id


def test_v07_full_durable_runtime_is_multi_restart_equivalent():
    continuous_store, continuous_id = run_continuous()
    restarted_store, restarted_id = run_with_three_restarts()

    assert durable_snapshot(restarted_store, restarted_id) == durable_snapshot(
        continuous_store,
        continuous_id,
    )

    assert restarted_store.entity("work_order", restarted_id).state == "closed"
    assert restarted_store.scheduled_work() == ()
    assert [r.request_id for r in restarted_store.resource_reservations()] == [
        "bay-holder"
    ]
    assert [r.request_id for r in restarted_store.preemptive_resource_reservations()] == [
        "emergency-crew"
    ]
    assert len(restarted_store.resource_preemption_results()) == 1
    assert restarted_store.store_items() == ()
    assert [r.request_id for r in restarted_store.store_get_results()] == [
        "consume-part-1"
    ]
    assert restarted_store.container_states()[0].level == 10.0
    assert [r.request_id for r in restarted_store.container_operation_results()] == [
        "consume-fuel"
    ]

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
from sose.scenarios import AttributeEffect, EventTrigger, Scenario

ORIGIN = datetime(2026, 1, 1, 8, tzinfo=timezone.utc)


def build(store: MemoryPersistence, *, now: datetime) -> tuple[SimulationContext, Engine]:
    context = SimulationContext(
        clock=SimulationClock(now=now, step=timedelta(hours=1)),
        random=RandomSource(42),
        scheduler=Scheduler(),
    )
    registry = DomainRegistry()
    registry.register(EntityType("work_order", WorkOrderChart))
    scenario = Scenario(
        name="runtime-marker",
        trigger=EventTrigger(
            event="entity.state_transition",
            entity_type="work_order",
        ),
        duration=timedelta(hours=8),
        effects=(AttributeEffect("runtime.marker", "active"),),
    )
    return context, Engine(
        context=context,
        registry=registry,
        persistence=store,
        scenarios=(scenario,),
    )


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
    engine.stores.define(StoreDefinition("inbox", kind="fifo", capacity=1))
    engine.stores.define(StoreDefinition("buffer", kind="fifo", capacity=1))
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
    engine.resources.request(
        backend,
        resource_name="bay",
        request_id="bay-waiter",
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

    engine.preemptive_resources.request(
        backend,
        resource_name="crew",
        request_id="standby-crew",
        requested_at=ORIGIN,
        priority=0,
        preempt=False,
    )
    backend.run_until(ORIGIN)

    engine.preemptive_resources.request(
        backend,
        resource_name="crew",
        request_id="backup-crew",
        requested_at=ORIGIN,
        priority=75,
        preempt=False,
    )
    backend.run_until(ORIGIN)

    engine.stores.get(
        backend,
        store_name="inbox",
        request_id="await-part",
        requested_at=ORIGIN,
    )
    backend.run_until(ORIGIN)

    engine.stores.put(
        backend,
        store_name="buffer",
        item_id="buffer-part-1",
        value={"sku": "buffer-bearing-1"},
        requested_at=ORIGIN,
    )
    backend.run_until(ORIGIN)
    engine.stores.put(
        backend,
        store_name="buffer",
        item_id="buffer-part-2",
        value={"sku": "buffer-bearing-2"},
        requested_at=ORIGIN,
    )
    backend.run_until(ORIGIN)

    engine.containers.get(
        backend,
        container_name="fuel",
        request_id="consume-fuel",
        amount=30.0,
        requested_at=ORIGIN,
    )
    backend.run_until(ORIGIN)

    assert store.entity("work_order", work_order_id).state == "planned"
    assert [d.request_id for d in store.resource_demands()] == ["bay-waiter"]
    assert [d.request_id for d in store.preemptive_resource_demands()] == [
        "standby-crew",
        "backup-crew",
    ]
    standby, backup = store.preemptive_resource_demands()
    assert (standby.priority, standby.preempt) == (0, False)
    assert (backup.priority, backup.preempt) == (75, False)
    assert [r.request_id for r in store.store_get_requests()] == ["await-part"]
    assert [i.item_id for i in store.store_put_intents()] == ["buffer-part-2"]
    assert [i.request_id for i in store.container_operation_intents()] == [
        "consume-fuel"
    ]
    return engine, backend


def assert_rebuilt_backend_pending_state(backend: SimPyBackend) -> None:
    assert backend.resource_snapshot("bay").in_use == 1
    assert backend.resource_snapshot("bay").queued == 1
    assert backend.preemptive_resource_snapshot("crew").in_use == 1
    assert backend.preemptive_resource_snapshot("crew").queued == 2
    assert backend.store_snapshot("inbox").size == 0
    assert backend.store_snapshot("inbox").queued_gets == 1
    assert backend.store_snapshot("inbox").queued_puts == 0
    assert backend.store_snapshot("buffer").size == 1
    assert backend.store_snapshot("buffer").queued_gets == 0
    assert backend.store_snapshot("buffer").queued_puts == 1
    assert backend.container_snapshot("fuel").level == 20.0
    assert backend.container_snapshot("fuel").queued_gets == 1


def complete_pending_operations(
    store: MemoryPersistence,
    engine: Engine,
    backend: SimPyBackend,
) -> None:
    holder = next(
        reservation
        for reservation in store.resource_reservations()
        if reservation.request_id == "bay-holder"
    )
    engine.resources.release(backend, holder.reservation_id)

    emergency = next(
        reservation
        for reservation in store.preemptive_resource_reservations()
        if reservation.request_id == "emergency-crew"
    )
    engine.preemptive_resources.release(backend, emergency.reservation_id)

    engine.stores.put(
        backend,
        store_name="inbox",
        item_id="part-after-restart",
        value={"sku": "bearing-after-restart"},
        requested_at=backend.now,
    )
    engine.stores.get(
        backend,
        store_name="buffer",
        request_id="consume-buffer-part-1",
        requested_at=backend.now,
    )

    engine.containers.put(
        backend,
        container_name="fuel",
        request_id="refuel",
        amount=20.0,
        requested_at=backend.now,
    )
    backend.run_until(backend.now)

    assert [r.request_id for r in store.resource_reservations()] == ["bay-waiter"]
    assert store.resource_demands() == ()
    assert [r.request_id for r in store.preemptive_resource_reservations()] == [
        "standby-crew"
    ]
    assert [d.request_id for d in store.preemptive_resource_demands()] == [
        "backup-crew"
    ]
    assert [i.item_id for i in store.store_items()] == ["buffer-part-2"]
    assert store.store_put_intents() == ()
    assert store.store_get_requests() == ()
    assert [r.request_id for r in store.store_get_results()] == [
        "await-part",
        "consume-buffer-part-1",
    ]
    assert store.container_operation_intents() == ()
    assert store.container_states()[0].level == 10.0

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
    engine, backend = start_operational_state(store, work_order_id)
    backend.run_until(ORIGIN + timedelta(hours=1))
    scenario_state = store.scenario_state()
    assert scenario_state is not None
    assert "runtime-marker" in scenario_state.decisions
    assert "runtime-marker" in scenario_state.activations
    assert scenario_state.activations["runtime-marker"].active is True
    assert scenario_state.activations["runtime-marker"].effects["runtime.marker"] == "active"
    complete_pending_operations(store, engine, backend)
    backend.run_until(ORIGIN + timedelta(hours=4))
    return store, work_order_id


def run_with_three_restarts() -> tuple[MemoryPersistence, str]:
    store = MemoryPersistence()
    work_order_id = seed(store)
    _, backend1 = start_operational_state(store, work_order_id)

    backend1.run_until(ORIGIN + timedelta(hours=1))
    scenario_state = store.scenario_state()
    assert scenario_state is not None
    assert "runtime-marker" in scenario_state.decisions
    assert "runtime-marker" in scenario_state.activations
    assert scenario_state.activations["runtime-marker"].active is True
    assert scenario_state.activations["runtime-marker"].effects["runtime.marker"] == "active"

    position = store.simulation_position()
    assert position is not None
    _, engine2 = build(store, now=position.logical_time)
    backend2 = SimPyBackend(origin=position.logical_time)
    engine2.rebuild_backend(backend2)
    backend2.run_until(position.logical_time)
    assert_rebuilt_backend_pending_state(backend2)
    complete_pending_operations(store, engine2, backend2)

    backend2.run_until(ORIGIN + timedelta(hours=2))
    for boundary in (3, 4):
        position = store.simulation_position()
        assert position is not None
        _, engine = build(store, now=position.logical_time)
        backend = SimPyBackend(origin=position.logical_time)
        engine.rebuild_backend(backend)
        backend.run_until(position.logical_time)

        assert backend.resource_snapshot("bay").in_use == 1
        assert backend.resource_snapshot("bay").queued == 0
        assert backend.preemptive_resource_snapshot("crew").in_use == 1
        assert backend.preemptive_resource_snapshot("crew").queued == 1
        assert backend.store_snapshot("inbox").size == 0
        assert backend.store_snapshot("inbox").queued_puts == 0
        assert backend.store_snapshot("inbox").queued_gets == 0
        assert backend.store_snapshot("buffer").size == 1
        assert backend.store_snapshot("buffer").queued_puts == 0
        assert backend.store_snapshot("buffer").queued_gets == 0
        assert backend.container_snapshot("fuel").level == 10.0

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
        "bay-waiter"
    ]
    assert [r.request_id for r in restarted_store.preemptive_resource_reservations()] == [
        "standby-crew"
    ]
    assert len(restarted_store.resource_preemption_results()) == 1
    assert [d.request_id for d in restarted_store.preemptive_resource_demands()] == [
        "backup-crew"
    ]
    assert [i.item_id for i in restarted_store.store_items()] == [
        "buffer-part-2"
    ]
    assert [r.request_id for r in restarted_store.store_get_results()] == [
        "await-part",
        "consume-buffer-part-1",
    ]
    assert restarted_store.container_states()[0].level == 10.0
    assert [r.request_id for r in restarted_store.container_operation_results()] == [
        "consume-fuel",
        "refuel",
    ]
    assert restarted_store.scenario_state() is not None

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sose.backends.simpy import SimPyBackend
from sose.core.clock import SimulationClock
from sose.core.context import SimulationContext
from sose.core.engine import Engine
from sose.core.identity import deterministic_id
from sose.core.randomness import RandomSource
from sose.core.runtime import (
    ContainerDefinition,
    PreemptiveResourceDefinition,
    ResourceDefinition,
    StoreDefinition,
)
from sose.core.scheduler import Scheduler
from sose.domain.registry import DomainRegistry, EntityType
from sose.persistence.memory import MemoryPersistence

from .entities import Operation, ProductionOrder
from .statecharts import OperationChart, ProductionOrderChart


ORIGIN = datetime(2026, 2, 1, 8, tzinfo=timezone.utc)
SKU = "finished-widget-a"
RAW_SKU = "steel-lot-a"
MATERIAL_CAPACITY = 1_000.0
FINISHED_CAPACITY = 1_000.0


@dataclass(frozen=True, slots=True)
class ManufacturingEntities:
    production_order_id: str
    operation_id: str


def flow_correlation_id() -> str:
    return deterministic_id("manufacturing-flow", "reference", "po-1")


def build_runtime(
    persistence: MemoryPersistence,
    *,
    now: datetime = ORIGIN,
    tick: int = 0,
    scenarios=(),
) -> tuple[SimulationContext, Engine]:
    context = SimulationContext(
        clock=SimulationClock(now=now, step=timedelta(hours=1), tick=tick),
        random=RandomSource(root_seed=84),
        scheduler=Scheduler(),
    )
    registry = DomainRegistry()
    registry.register(EntityType("production_order", ProductionOrderChart))
    registry.register(EntityType("manufacturing_operation", OperationChart))
    return context, Engine(
        context=context,
        registry=registry,
        persistence=persistence,
        scenarios=scenarios,
    )


def _validate_quantity(quantity: float) -> None:
    if quantity <= 0 or quantity > min(MATERIAL_CAPACITY, FINISHED_CAPACITY):
        raise ValueError("quantity must fit material and finished-goods capacity")


def seed_happy_path(
    persistence: MemoryPersistence,
    *,
    quantity: float = 10.0,
) -> ManufacturingEntities:
    _validate_quantity(quantity)
    context, engine = build_runtime(persistence)
    correlation_id = flow_correlation_id()

    order = context.entities.create(
        ProductionOrder,
        key=("manufacturing-reference", "po-1"),
        state="planned",
        attributes={"sku": SKU, "quantity": quantity},
    )
    operation = context.entities.create(
        Operation,
        key=("manufacturing-reference", "op-10"),
        state="pending",
        attributes={"work_center": "wc-10", "quantity": quantity},
    )
    with persistence.transaction() as uow:
        uow.save_entity(order)
        uow.save_entity(operation)
        uow.save_resource_definition(ResourceDefinition("operator", capacity=1))

    engine.preemptive_resources.define(
        PreemptiveResourceDefinition("machine", capacity=1)
    )
    engine.stores.define(StoreDefinition("raw_material_lots", kind="fifo", capacity=10))
    engine.stores.define(StoreDefinition("wip_buffer", kind="fifo", capacity=10))
    engine.containers.define(
        ContainerDefinition("raw_material", capacity=MATERIAL_CAPACITY, initial=0.0)
    )
    engine.containers.define(
        ContainerDefinition("finished_goods", capacity=FINISHED_CAPACITY, initial=0.0)
    )

    previous = None
    for entity, trigger, offset in (
        (order, "release", timedelta(hours=1)),
        (operation, "ready", timedelta(hours=1)),
    ):
        command = context.commands.create(
            trigger,
            target=entity,
            due_at=ORIGIN + offset,
            caused_by=previous,
            correlation_id=correlation_id,
            key=("manufacturing-reference", entity.entity_type, entity.id, trigger),
        )
        context.schedules.at(command.due_at, command=command)
        previous = command

    return ManufacturingEntities(order.id, operation.id)



def _preemptive_request_exists(persistence: MemoryPersistence, request_id: str) -> bool:
    return any(
        demand.request_id == request_id
        for demand in persistence.preemptive_resource_demands()
    ) or any(
        reservation.request_id == request_id
        for reservation in persistence.preemptive_resource_reservations()
    )


def _normal_request_exists(persistence: MemoryPersistence, request_id: str) -> bool:
    return any(
        demand.request_id == request_id for demand in persistence.resource_demands()
    ) or any(
        reservation.request_id == request_id
        for reservation in persistence.resource_reservations()
    )


def _preemptive_reservation(persistence: MemoryPersistence, request_id: str):
    return next(
        (
            reservation
            for reservation in persistence.preemptive_resource_reservations()
            if reservation.request_id == request_id
        ),
        None,
    )


def _resource_reservation(persistence: MemoryPersistence, request_id: str):
    return next(
        (
            reservation
            for reservation in persistence.resource_reservations()
            if reservation.request_id == request_id
        ),
        None,
    )


def reconcile_setup_resources(
    persistence: MemoryPersistence,
    engine: Engine,
    backend: SimPyBackend,
    *,
    entities: ManufacturingEntities,
) -> bool:
    """Advance into setup only while machine and operator are durably held."""

    machine_request = f"machine:{entities.production_order_id}"
    operator_request = f"operator:{entities.production_order_id}"

    if not _preemptive_request_exists(persistence, machine_request):
        engine.preemptive_resources.request(
            backend,
            resource_name="machine",
            request_id=machine_request,
            requested_at=backend.now,
            priority=100,
            preempt=False,
        )
    if not _normal_request_exists(persistence, operator_request):
        engine.resources.request(
            backend,
            resource_name="operator",
            request_id=operator_request,
            requested_at=backend.now,
            priority=100,
        )
    backend.run_until(backend.now)

    machine = _preemptive_reservation(persistence, machine_request)
    operator = _resource_reservation(persistence, operator_request)
    if machine is None or operator is None:
        return False

    order = persistence.entity("production_order", entities.production_order_id)
    operation = persistence.entity("manufacturing_operation", entities.operation_id)
    if order is None or operation is None:
        raise RuntimeError("manufacturing entities were not persisted")

    if order.state == "released":
        command = engine.context.commands.create(
            "begin_setup",
            target=order,
            correlation_id=flow_correlation_id(),
            key=("manufacturing-reference", order.id, "begin-setup"),
        )
        engine.dispatch(command)

    if operation.state == "ready_state":
        command = engine.context.commands.create(
            "start",
            target=operation,
            correlation_id=flow_correlation_id(),
            key=("manufacturing-reference", operation.id, "start"),
        )
        engine.dispatch(command)

    return True


def release_setup_resources(
    persistence: MemoryPersistence,
    engine: Engine,
    backend: SimPyBackend,
    *,
    entities: ManufacturingEntities,
) -> None:
    machine = _preemptive_reservation(
        persistence, f"machine:{entities.production_order_id}"
    )
    if machine is not None:
        engine.preemptive_resources.release(backend, machine.reservation_id)
        backend.run_until(backend.now)

    operator = _resource_reservation(
        persistence, f"operator:{entities.production_order_id}"
    )
    if operator is not None:
        engine.resources.release(backend, operator.reservation_id)
        backend.run_until(backend.now)


def seed_material(
    engine: Engine,
    backend: SimPyBackend,
    *,
    quantity: float,
) -> None:
    _validate_quantity(quantity)
    if not any(i.item_id == "raw-lot-1" for i in engine.persistence.store_items()):
        engine.stores.put(
            backend,
            store_name="raw_material_lots",
            item_id="raw-lot-1",
            value={"sku": RAW_SKU, "quantity": quantity},
            requested_at=backend.now,
        )
    if not any(
        i.request_id == "seed-raw-material"
        for i in engine.persistence.container_operation_intents()
    ) and not any(
        r.request_id == "seed-raw-material"
        for r in engine.persistence.container_operation_results()
    ):
        engine.containers.put(
            backend,
            container_name="raw_material",
            request_id="seed-raw-material",
            amount=quantity,
            requested_at=backend.now,
        )
    backend.run_until(backend.now)



def reconcile_breakdown(
    persistence: MemoryPersistence,
    engine: Engine,
    backend: SimPyBackend,
    *,
    entities: ManufacturingEntities,
) -> bool:
    """Preempt production capacity and make the breakdown durable in business state."""

    repair_request = f"machine-repair:{entities.production_order_id}"
    if not _preemptive_request_exists(persistence, repair_request):
        engine.preemptive_resources.request(
            backend,
            resource_name="machine",
            request_id=repair_request,
            requested_at=backend.now,
            priority=1,
            preempt=True,
        )
    backend.run_until(backend.now)

    repair = _preemptive_reservation(persistence, repair_request)
    if repair is None:
        return False

    preempted = any(
        result.preempting_request_id == repair_request
        for result in persistence.resource_preemption_results()
    )
    if not preempted:
        return False

    order = persistence.entity("production_order", entities.production_order_id)
    operation = persistence.entity("manufacturing_operation", entities.operation_id)
    if order is None or operation is None:
        raise RuntimeError("manufacturing entities were not persisted")

    if order.state in {"setup", "producing"}:
        command = engine.context.commands.create(
            "breakdown",
            target=order,
            correlation_id=flow_correlation_id(),
            key=("manufacturing-breakdown", order.id, "breakdown"),
        )
        engine.dispatch(command)

    operation = persistence.entity("manufacturing_operation", entities.operation_id)
    if operation is not None and operation.state in {"ready_state", "running"}:
        command = engine.context.commands.create(
            "block",
            target=operation,
            correlation_id=flow_correlation_id(),
            key=("manufacturing-breakdown", operation.id, "block"),
        )
        engine.dispatch(command)

    return True


def reconcile_repair(
    persistence: MemoryPersistence,
    engine: Engine,
    backend: SimPyBackend,
    *,
    entities: ManufacturingEntities,
) -> bool:
    """Release emergency repair and reacquire machine before resuming production."""

    repair_request = f"machine-repair:{entities.production_order_id}"
    production_request = f"machine:{entities.production_order_id}"

    repair = _preemptive_reservation(persistence, repair_request)
    if repair is not None:
        engine.preemptive_resources.release(backend, repair.reservation_id)
        backend.run_until(backend.now)

    if not _preemptive_request_exists(persistence, production_request):
        engine.preemptive_resources.request(
            backend,
            resource_name="machine",
            request_id=production_request,
            requested_at=backend.now,
            priority=100,
            preempt=False,
        )
    backend.run_until(backend.now)

    machine = _preemptive_reservation(persistence, production_request)
    if machine is None:
        return False

    order = persistence.entity("production_order", entities.production_order_id)
    operation = persistence.entity("manufacturing_operation", entities.operation_id)
    if order is None or operation is None:
        raise RuntimeError("manufacturing entities were not persisted")

    if order.state == "machine_down":
        command = engine.context.commands.create(
            "repair",
            target=order,
            correlation_id=flow_correlation_id(),
            key=("manufacturing-breakdown", order.id, "repair"),
        )
        engine.dispatch(command)

    operation = persistence.entity("manufacturing_operation", entities.operation_id)
    if operation is not None and operation.state == "blocked":
        command = engine.context.commands.create(
            "unblock",
            target=operation,
            correlation_id=flow_correlation_id(),
            key=("manufacturing-breakdown", operation.id, "unblock"),
        )
        engine.dispatch(command)
        operation = persistence.entity("manufacturing_operation", entities.operation_id)

    if operation is not None and operation.state == "ready_state":
        command = engine.context.commands.create(
            "start",
            target=operation,
            correlation_id=flow_correlation_id(),
            key=("manufacturing-breakdown", operation.id, "restart"),
        )
        engine.dispatch(command)

    return True


def reconcile_material_availability(
    persistence: MemoryPersistence,
    engine: Engine,
    *,
    entities: ManufacturingEntities,
) -> None:
    """Keep shortage visible in durable business state until replenishment exists."""

    order = persistence.entity("production_order", entities.production_order_id)
    if order is None:
        raise RuntimeError("production order was not persisted")

    has_lot = bool(persistence.store_items())
    raw_level = next(
        (state.level for state in persistence.container_states() if state.name == "raw_material"),
        0.0,
    )
    if order.state == "released" and (not has_lot or raw_level <= 0):
        command = engine.context.commands.create(
            "wait_for_material",
            target=order,
            correlation_id=flow_correlation_id(),
            key=("manufacturing-reference", order.id, "wait-material"),
        )
        engine.dispatch(command)
    elif order.state == "waiting_material" and has_lot and raw_level > 0:
        command = engine.context.commands.create(
            "material_ready",
            target=order,
            correlation_id=flow_correlation_id(),
            key=("manufacturing-reference", order.id, "material-ready"),
        )
        engine.dispatch(command)


def reconcile_material_issue(
    persistence: MemoryPersistence,
    engine: Engine,
    backend: SimPyBackend,
    *,
    entities: ManufacturingEntities,
    quantity: float,
) -> None:
    _validate_quantity(quantity)
    lot_request = "issue-raw-lot-1"
    qty_request = "issue-raw-material-1"

    if not any(r.request_id == lot_request for r in persistence.store_get_requests()) and not any(
        r.request_id == lot_request for r in persistence.store_get_results()
    ):
        engine.stores.get(
            backend,
            store_name="raw_material_lots",
            request_id=lot_request,
            requested_at=backend.now,
        )
    if not any(i.request_id == qty_request for i in persistence.container_operation_intents()) and not any(
        r.request_id == qty_request for r in persistence.container_operation_results()
    ):
        engine.containers.get(
            backend,
            container_name="raw_material",
            request_id=qty_request,
            amount=quantity,
            requested_at=backend.now,
        )
    backend.run_until(backend.now)

    lot_done = any(r.request_id == lot_request for r in persistence.store_get_results())
    qty_done = any(r.request_id == qty_request for r in persistence.container_operation_results())
    if not lot_done or not qty_done:
        raise RuntimeError("raw material issue is still pending")

    order = persistence.entity("production_order", entities.production_order_id)
    if order is None:
        raise RuntimeError("production order was not persisted")
    if order.state == "setup":
        command = engine.context.commands.create(
            "start_production",
            target=order,
            correlation_id=flow_correlation_id(),
            key=("manufacturing-reference", order.id, "start-production"),
        )
        engine.dispatch(command)


def reconcile_output(
    persistence: MemoryPersistence,
    engine: Engine,
    backend: SimPyBackend,
    *,
    entities: ManufacturingEntities,
    quantity: float,
) -> None:
    _validate_quantity(quantity)
    wip_id = "wip-1"
    fg_request = "finish-goods-1"

    if not any(i.item_id == wip_id for i in persistence.store_items()) and not any(
        i.item_id == wip_id for i in persistence.store_put_intents()
    ):
        engine.stores.put(
            backend,
            store_name="wip_buffer",
            item_id=wip_id,
            value={"sku": SKU, "quantity": quantity},
            requested_at=backend.now,
        )
    if not any(i.request_id == fg_request for i in persistence.container_operation_intents()) and not any(
        r.request_id == fg_request for r in persistence.container_operation_results()
    ):
        engine.containers.put(
            backend,
            container_name="finished_goods",
            request_id=fg_request,
            amount=quantity,
            requested_at=backend.now,
        )
    backend.run_until(backend.now)

    wip_done = any(i.item_id == wip_id for i in persistence.store_items())
    fg_done = any(r.request_id == fg_request for r in persistence.container_operation_results())
    if not wip_done or not fg_done:
        raise RuntimeError("production output is not durably committed")

    consume_wip = "consume-wip-1"
    if not any(r.request_id == consume_wip for r in persistence.store_get_requests()) and not any(
        r.request_id == consume_wip for r in persistence.store_get_results()
    ):
        engine.stores.get(
            backend,
            store_name="wip_buffer",
            request_id=consume_wip,
            requested_at=backend.now,
        )
    backend.run_until(backend.now)
    if not any(r.request_id == consume_wip for r in persistence.store_get_results()):
        raise RuntimeError("WIP transfer to finished goods is still pending")

    order = persistence.entity("production_order", entities.production_order_id)
    operation = persistence.entity("manufacturing_operation", entities.operation_id)
    if order is None or operation is None:
        raise RuntimeError("manufacturing entities were not persisted")

    if order.state == "producing":
        command = engine.context.commands.create(
            "begin_inspection",
            target=order,
            correlation_id=flow_correlation_id(),
            key=("manufacturing-reference", order.id, "inspect"),
        )
        engine.dispatch(command)
        order = persistence.entity("production_order", entities.production_order_id)

    if operation.state == "running":
        command = engine.context.commands.create(
            "finish",
            target=operation,
            correlation_id=flow_correlation_id(),
            key=("manufacturing-reference", operation.id, "finish"),
        )
        engine.dispatch(command)

    if order.state == "inspection":
        command = engine.context.commands.create(
            "complete",
            target=order,
            correlation_id=flow_correlation_id(),
            key=("manufacturing-reference", order.id, "complete"),
        )
        engine.dispatch(command)


def run_happy_path(
    *,
    quantity: float = 10.0,
) -> tuple[MemoryPersistence, ManufacturingEntities]:
    persistence = MemoryPersistence()
    entities = seed_happy_path(persistence, quantity=quantity)
    _, engine = build_runtime(persistence)
    backend = SimPyBackend(origin=ORIGIN)
    engine.rebuild_backend(backend)

    seed_material(engine, backend, quantity=quantity)
    backend.run_until(ORIGIN + timedelta(hours=1))
    reconcile_material_availability(
        persistence, engine, entities=entities
    )
    if not reconcile_setup_resources(
        persistence, engine, backend, entities=entities
    ):
        raise RuntimeError("manufacturing capacity is still unavailable")
    reconcile_material_issue(
        persistence, engine, backend, entities=entities, quantity=quantity
    )
    reconcile_output(
        persistence, engine, backend, entities=entities, quantity=quantity
    )
    release_setup_resources(
        persistence, engine, backend, entities=entities
    )
    return persistence, entities

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING
from datetime import datetime, timedelta, timezone

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

from .entities import PartDemand, WorkOrder
from .statecharts import PartDemandChart, WorkOrderChart

if TYPE_CHECKING:
    from sose.backends.simpy import SimPyBackend


ORIGIN = datetime(2026, 3, 1, 8, tzinfo=timezone.utc)
PART_SKU = "bearing-6204"
PART_CAPACITY = 100.0


@dataclass(frozen=True, slots=True)
class MROEntities:
    work_order_id: str
    part_demand_id: str


def flow_correlation_id() -> str:
    return deterministic_id("mro-flow", "reference", "wo-1")


def build_runtime(
    persistence: MemoryPersistence,
    *,
    now: datetime = ORIGIN,
    tick: int = 0,
    scenarios=(),
) -> tuple[SimulationContext, Engine]:
    context = SimulationContext(
        clock=SimulationClock(now=now, step=timedelta(hours=1), tick=tick),
        random=RandomSource(root_seed=42),
        scheduler=Scheduler(),
    )
    registry = DomainRegistry()
    registry.register(EntityType("work_order", WorkOrderChart))
    registry.register(EntityType("part_demand", PartDemandChart))
    return context, Engine(
        context=context,
        registry=registry,
        persistence=persistence,
        scenarios=scenarios,
    )


def seed_reference(
    persistence: MemoryPersistence,
    *,
    quantity: float = 1.0,
) -> MROEntities:
    if quantity <= 0 or quantity > PART_CAPACITY:
        raise ValueError("quantity must fit spare-parts capacity")

    context, engine = build_runtime(persistence)
    wo = context.entities.create(
        WorkOrder,
        key=("mro-reference", "wo-1"),
        state="planned",
        attributes={"priority": "HIGH", "part_sku": PART_SKU, "quantity": quantity},
    )
    demand = context.entities.create(
        PartDemand,
        key=("mro-reference", "part-demand-1"),
        state="open",
        attributes={"sku": PART_SKU, "quantity": quantity},
    )
    with persistence.transaction() as uow:
        uow.save_entity(wo)
        uow.save_entity(demand)
        uow.save_resource_definition(ResourceDefinition("technician", capacity=1))

    engine.preemptive_resources.define(
        PreemptiveResourceDefinition("maintenance_bay", capacity=1)
    )
    engine.stores.define(StoreDefinition("spare_part_lots", kind="fifo", capacity=10))
    engine.containers.define(
        ContainerDefinition("spare_parts", capacity=PART_CAPACITY, initial=0.0)
    )

    release = context.commands.create(
        "release",
        target=wo,
        due_at=ORIGIN + timedelta(hours=1),
        correlation_id=flow_correlation_id(),
        key=("mro-reference", wo.id, "release"),
    )
    context.schedules.at(release.due_at, command=release)
    return MROEntities(wo.id, demand.id)


def build_demo() -> tuple[Engine, MemoryPersistence, str]:
    persistence = MemoryPersistence()
    ids = seed_reference(persistence)
    _, engine = build_runtime(persistence)
    return engine, persistence, ids.work_order_id


def seed_spare_parts(
    engine: Engine,
    backend: SimPyBackend,
    *,
    quantity: float,
) -> None:
    if quantity <= 0 or quantity > PART_CAPACITY:
        raise ValueError("quantity must fit spare-parts capacity")

    if not any(item.item_id == "part-lot-1" for item in engine.persistence.store_items()):
        engine.stores.put(
            backend,
            store_name="spare_part_lots",
            item_id="part-lot-1",
            value={"sku": PART_SKU, "quantity": quantity},
            requested_at=backend.now,
        )

    if not any(
        intent.request_id == "seed-spare-parts"
        for intent in engine.persistence.container_operation_intents()
    ) and not any(
        result.request_id == "seed-spare-parts"
        for result in engine.persistence.container_operation_results()
    ):
        engine.containers.put(
            backend,
            container_name="spare_parts",
            request_id="seed-spare-parts",
            amount=quantity,
            requested_at=backend.now,
        )
    backend.run_until(backend.now)


def _resource_request_exists(persistence, request_id: str) -> bool:
    return any(d.request_id == request_id for d in persistence.resource_demands()) or any(
        r.request_id == request_id for r in persistence.resource_reservations()
    )


def _preemptive_request_exists(persistence, request_id: str) -> bool:
    return any(
        d.request_id == request_id for d in persistence.preemptive_resource_demands()
    ) or any(
        r.request_id == request_id
        for r in persistence.preemptive_resource_reservations()
    )


def _resource_reservation(persistence, request_id: str):
    return next(
        (r for r in persistence.resource_reservations() if r.request_id == request_id),
        None,
    )


def _preemptive_reservation(persistence, request_id: str):
    return next(
        (
            r
            for r in persistence.preemptive_resource_reservations()
            if r.request_id == request_id
        ),
        None,
    )


def _part_issue_complete(persistence: MemoryPersistence) -> bool:
    lot_done = any(
        result.request_id == "consume-part-lot-1"
        for result in persistence.store_get_results()
    )
    qty_done = any(
        result.request_id == "consume-spare-part-1"
        for result in persistence.container_operation_results()
    )
    return lot_done and qty_done


def _discard_granted_capacity_if_terminal(
    persistence: MemoryPersistence,
    engine: Engine,
    backend: "SimPyBackend",
    *,
    work_order_id: str,
    reservation,
    preemptive: bool,
) -> None:
    work_order = persistence.entity("work_order", work_order_id)
    if work_order is None or work_order.state not in {"cancelled", "closed"}:
        return
    if preemptive:
        engine.preemptive_resources.release(backend, reservation.reservation_id)
    else:
        engine.resources.release(backend, reservation.reservation_id)


def reconcile_material_availability(
    persistence: MemoryPersistence,
    engine: Engine,
    *,
    entities: MROEntities,
    quantity: float,
) -> bool:
    """Expose spare-part shortage before acquiring constrained maintenance capacity."""

    if _part_issue_complete(persistence):
        return True

    available = next(
        (s.level for s in persistence.container_states() if s.name == "spare_parts"),
        0.0,
    )
    wo = persistence.entity("work_order", entities.work_order_id)
    demand = persistence.entity("part_demand", entities.part_demand_id)
    if wo is None or demand is None:
        raise RuntimeError("MRO entities were not persisted")

    if available < quantity:
        if wo.state == "released":
            engine.dispatch(
                engine.context.commands.create(
                    "wait_for_material",
                    target=wo,
                    correlation_id=flow_correlation_id(),
                    key=("mro", wo.id, "wait-material"),
                )
            )
        if demand.state == "open":
            engine.dispatch(
                engine.context.commands.create(
                    "wait",
                    target=demand,
                    correlation_id=flow_correlation_id(),
                    key=("mro", demand.id, "wait"),
                )
            )
        return False

    if wo.state == "waiting_material":
        engine.dispatch(
            engine.context.commands.create(
                "material_ready",
                target=wo,
                correlation_id=flow_correlation_id(),
                key=("mro", wo.id, "material-ready"),
            )
        )
    return True


def reconcile_capacity(
    persistence: MemoryPersistence,
    engine: Engine,
    backend: SimPyBackend,
    *,
    entities: MROEntities,
) -> bool:
    tech_id = f"technician:{entities.work_order_id}"
    bay_id = f"bay:{entities.work_order_id}"

    if not _resource_request_exists(persistence, tech_id):
        engine.resources.request(
            backend,
            resource_name="technician",
            request_id=tech_id,
            requested_at=backend.now,
            priority=100,
            on_acquired=lambda reservation: _discard_granted_capacity_if_terminal(
                persistence,
                engine,
                backend,
                work_order_id=entities.work_order_id,
                reservation=reservation,
                preemptive=False,
            ),
        )
    if not _preemptive_request_exists(persistence, bay_id):
        engine.preemptive_resources.request(
            backend,
            resource_name="maintenance_bay",
            request_id=bay_id,
            requested_at=backend.now,
            priority=100,
            preempt=False,
            on_acquired=lambda reservation: _discard_granted_capacity_if_terminal(
                persistence,
                engine,
                backend,
                work_order_id=entities.work_order_id,
                reservation=reservation,
                preemptive=True,
            ),
        )
    backend.run_until(backend.now)

    return (
        _resource_reservation(persistence, tech_id) is not None
        and _preemptive_reservation(persistence, bay_id) is not None
    )


def reconcile_part_issue(
    persistence: MemoryPersistence,
    engine: Engine,
    backend: SimPyBackend,
    *,
    entities: MROEntities,
    quantity: float,
) -> bool:
    wo = persistence.entity("work_order", entities.work_order_id)
    demand = persistence.entity("part_demand", entities.part_demand_id)
    if wo is None or demand is None:
        raise RuntimeError("MRO entities were not persisted")

    already_issued = _part_issue_complete(persistence)
    available = next(
        (s.level for s in persistence.container_states() if s.name == "spare_parts"),
        0.0,
    )
    if not already_issued and available < quantity:
        if wo.state == "released":
            engine.dispatch(
                engine.context.commands.create(
                    "wait_for_material",
                    target=wo,
                    correlation_id=flow_correlation_id(),
                    key=("mro", wo.id, "wait-material"),
                )
            )
        if demand.state == "open":
            engine.dispatch(
                engine.context.commands.create(
                    "wait",
                    target=demand,
                    correlation_id=flow_correlation_id(),
                    key=("mro", demand.id, "wait"),
                )
            )
        return False

    lot_request = "consume-part-lot-1"
    qty_request = "consume-spare-part-1"
    if already_issued:
        lot_done = qty_done = True
    else:
        lot_done = qty_done = False

    if not already_issued and not any(r.request_id == lot_request for r in persistence.store_get_requests()) and not any(
        r.request_id == lot_request for r in persistence.store_get_results()
    ):
        engine.stores.get(
            backend,
            store_name="spare_part_lots",
            request_id=lot_request,
            requested_at=backend.now,
        )
    if not already_issued and not any(i.request_id == qty_request for i in persistence.container_operation_intents()) and not any(
        r.request_id == qty_request for r in persistence.container_operation_results()
    ):
        engine.containers.get(
            backend,
            container_name="spare_parts",
            request_id=qty_request,
            amount=quantity,
            requested_at=backend.now,
        )
    backend.run_until(backend.now)

    lot_done = lot_done or any(
        r.request_id == lot_request for r in persistence.store_get_results()
    )
    qty_done = qty_done or any(
        r.request_id == qty_request for r in persistence.container_operation_results()
    )
    if not lot_done or not qty_done:
        return False

    demand = persistence.entity("part_demand", entities.part_demand_id)
    if demand.state in {"open", "waiting_inventory"}:
        engine.dispatch(
            engine.context.commands.create(
                "allocate",
                target=demand,
                correlation_id=flow_correlation_id(),
                key=("mro", demand.id, "allocate"),
            )
        )
        demand = persistence.entity("part_demand", entities.part_demand_id)
    if demand.state == "allocated":
        engine.dispatch(
            engine.context.commands.create(
                "consume",
                target=demand,
                correlation_id=flow_correlation_id(),
                key=("mro", demand.id, "consume"),
            )
        )
    return True


def reconcile_start(
    persistence: MemoryPersistence,
    engine: Engine,
    backend: SimPyBackend,
    *,
    entities: MROEntities,
    quantity: float,
) -> bool:
    wo = persistence.entity("work_order", entities.work_order_id)
    if wo is None:
        raise RuntimeError("work order was not persisted")
    if wo.state == "in_progress":
        return True
    if wo.state in {"completed", "closed", "cancelled", "interrupted"}:
        return False

    if not reconcile_material_availability(
        persistence,
        engine,
        entities=entities,
        quantity=quantity,
    ):
        return False

    if not reconcile_capacity(persistence, engine, backend, entities=entities):
        wo = persistence.entity("work_order", entities.work_order_id)
        if wo.state == "released":
            engine.dispatch(
                engine.context.commands.create(
                    "wait_for_resource",
                    target=wo,
                    correlation_id=flow_correlation_id(),
                    key=("mro", wo.id, "wait-resource"),
                )
            )
        return False

    wo = persistence.entity("work_order", entities.work_order_id)
    if wo.state == "waiting_resource":
        engine.dispatch(
            engine.context.commands.create(
                "resource_ready",
                target=wo,
                correlation_id=flow_correlation_id(),
                key=("mro", wo.id, "resource-ready"),
            )
        )

    if not reconcile_part_issue(
        persistence,
        engine,
        backend,
        entities=entities,
        quantity=quantity,
    ):
        return False

    wo = persistence.entity("work_order", entities.work_order_id)
    if wo.state == "released":
        engine.dispatch(
            engine.context.commands.create(
                "start",
                target=wo,
                correlation_id=flow_correlation_id(),
                key=("mro", wo.id, "start"),
            )
        )
    return persistence.entity("work_order", entities.work_order_id).state == "in_progress"


def reconcile_complete(
    persistence: MemoryPersistence,
    engine: Engine,
    *,
    entities: MROEntities,
) -> None:
    wo = persistence.entity("work_order", entities.work_order_id)
    if wo is None:
        raise RuntimeError("work order was not persisted")
    if wo.state == "in_progress":
        engine.dispatch(
            engine.context.commands.create(
                "complete",
                target=wo,
                correlation_id=flow_correlation_id(),
                key=("mro", wo.id, "complete"),
            )
        )
        wo = persistence.entity("work_order", entities.work_order_id)
    if wo.state == "completed":
        engine.dispatch(
            engine.context.commands.create(
                "close",
                target=wo,
                correlation_id=flow_correlation_id(),
                key=("mro", wo.id, "close"),
            )
        )


def release_capacity(
    persistence: MemoryPersistence,
    engine: Engine,
    backend: SimPyBackend,
    *,
    entities: MROEntities,
) -> None:
    tech = _resource_reservation(persistence, f"technician:{entities.work_order_id}")
    if tech is not None:
        engine.resources.release(backend, tech.reservation_id)
        backend.run_until(backend.now)
    bay = _preemptive_reservation(persistence, f"bay:{entities.work_order_id}")
    if bay is not None:
        engine.preemptive_resources.release(backend, bay.reservation_id)
        backend.run_until(backend.now)


def reconcile_cancel(
    persistence: MemoryPersistence,
    engine: Engine,
    backend: SimPyBackend,
    *,
    entities: MROEntities,
) -> None:
    """Cancel outstanding work without leaving capacity or inventory side effects."""

    wo = persistence.entity("work_order", entities.work_order_id)
    demand = persistence.entity("part_demand", entities.part_demand_id)
    if wo is None or demand is None:
        raise RuntimeError("MRO entities were not persisted")

    if wo.state == "planned":
        with persistence.transaction() as uow:
            for work in persistence.scheduled_work():
                command = persistence.command(work.command_id)
                if (
                    command is not None
                    and command.entity_type == "work_order"
                    and command.entity_id == wo.id
                ):
                    uow.delete_scheduled_work(work.work_id)
                    uow.delete_command(command.command_id)

    if wo.state in {"planned", "released", "waiting_material", "waiting_resource"}:
        engine.dispatch(
            engine.context.commands.create(
                "cancel",
                target=wo,
                correlation_id=flow_correlation_id(),
                key=("mro", wo.id, "cancel"),
            )
        )

    demand = persistence.entity("part_demand", entities.part_demand_id)
    if demand.state in {"open", "waiting_inventory"}:
        engine.dispatch(
            engine.context.commands.create(
                "cancel",
                target=demand,
                correlation_id=flow_correlation_id(),
                key=("mro", demand.id, "cancel"),
            )
        )

    release_capacity(persistence, engine, backend, entities=entities)


def run_happy_path(
    *,
    quantity: float = 1.0,
) -> tuple[MemoryPersistence, MROEntities]:
    from sose.backends.simpy import SimPyBackend

    persistence = MemoryPersistence()
    ids = seed_reference(persistence, quantity=quantity)
    _, engine = build_runtime(persistence)
    backend = SimPyBackend(origin=ORIGIN)
    engine.rebuild_backend(backend)
    seed_spare_parts(engine, backend, quantity=quantity)
    backend.run_until(ORIGIN + timedelta(hours=1))

    if not reconcile_start(
        persistence, engine, backend, entities=ids, quantity=quantity
    ):
        raise RuntimeError("MRO prerequisites are still unavailable")
    reconcile_complete(persistence, engine, entities=ids)
    release_capacity(persistence, engine, backend, entities=ids)
    return persistence, ids

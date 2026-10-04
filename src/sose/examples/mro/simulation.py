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
    step: timedelta = timedelta(hours=1),
    random_seed: int = 42,
) -> tuple[SimulationContext, Engine]:
    context = SimulationContext(
        clock=SimulationClock(now=now, step=step, tick=tick),
        random=RandomSource(root_seed=random_seed),
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
    now: datetime = ORIGIN,
    technician_capacity: int = 1,
    maintenance_bay_capacity: int = 1,
    spare_part_store_capacity: int = 10,
    release_delay: timedelta = timedelta(hours=1),
) -> MROEntities:
    if quantity <= 0 or quantity > PART_CAPACITY:
        raise ValueError("quantity must fit spare-parts capacity")

    context, engine = build_runtime(persistence, now=now)
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
        uow.save_resource_definition(
            ResourceDefinition("technician", capacity=technician_capacity)
        )

    engine.preemptive_resources.define(
        PreemptiveResourceDefinition(
            "maintenance_bay",
            capacity=maintenance_bay_capacity,
        )
    )
    engine.stores.define(
        StoreDefinition(
            "spare_part_lots",
            kind="fifo",
            capacity=spare_part_store_capacity,
        )
    )
    engine.containers.define(
        ContainerDefinition("spare_parts", capacity=PART_CAPACITY, initial=0.0)
    )

    release = context.commands.create(
        "release",
        target=wo,
        due_at=now + release_delay,
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
        engine.preemptive_resources.withdraw(backend, reservation.request_id)
    else:
        engine.resources.withdraw(backend, reservation.request_id)


def reconcile_material_availability(
    persistence: MemoryPersistence,
    engine: Engine,
    backend: SimPyBackend,
    *,
    entities: MROEntities,
    quantity: float,
) -> bool:
    """Expose spare-part shortage before acquiring constrained maintenance capacity."""

    if _part_issue_complete(persistence):
        return True

    lot_available, available = _available_spare_parts(persistence, engine)
    wo = persistence.entity("work_order", entities.work_order_id)
    demand = persistence.entity("part_demand", entities.part_demand_id)
    if wo is None or demand is None:
        raise RuntimeError("MRO entities were not persisted")

    if not _material_is_available(lot_available, available, quantity):
        _reconcile_material_shortage(
            persistence,
            engine,
            backend,
            entities=entities,
            wo=wo,
            demand=demand,
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
    if not engine.context.scenarios.attribute("mro.technician.available", True):
        return False

    tech_id = f"technician:{entities.work_order_id}"
    bay_id = f"bay:{entities.work_order_id}"

    engine.resources.ensure_requested(
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
    engine.preemptive_resources.ensure_requested(
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

    return (
        engine.resources.reservation_for(tech_id) is not None
        and engine.preemptive_resources.reservation_for(bay_id) is not None
    )


def _can_issue_part_now(
    persistence: MemoryPersistence,
    *,
    quantity: float,
) -> bool:
    lot_available = any(item.item_id == "part-lot-1" for item in persistence.store_items())
    available = next(
        (s.level for s in persistence.container_states() if s.name == "spare_parts"),
        0.0,
    )
    return lot_available and available >= quantity


def _dispatch_material_wait(engine: Engine, *, wo, demand) -> None:
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


def _ensure_part_issue_requests(
    persistence: MemoryPersistence,
    engine: Engine,
    backend: SimPyBackend,
    *,
    quantity: float,
) -> None:
    lot_request = "consume-part-lot-1"
    qty_request = "consume-spare-part-1"

    lot_pending = any(r.request_id == lot_request for r in persistence.store_get_requests())
    lot_done = any(r.request_id == lot_request for r in persistence.store_get_results())
    if not lot_pending and not lot_done:
        engine.stores.get(
            backend,
            store_name="spare_part_lots",
            request_id=lot_request,
            requested_at=backend.now,
        )

    qty_pending = any(
        i.request_id == qty_request
        for i in persistence.container_operation_intents()
    )
    qty_done = any(
        r.request_id == qty_request
        for r in persistence.container_operation_results()
    )
    if not qty_pending and not qty_done:
        engine.containers.get(
            backend,
            container_name="spare_parts",
            request_id=qty_request,
            amount=quantity,
            requested_at=backend.now,
        )


def _dispatch_part_demand_progress(
    persistence: MemoryPersistence,
    engine: Engine,
    *,
    entities: MROEntities,
) -> None:
    demand = persistence.entity("part_demand", entities.part_demand_id)
    if demand is not None and demand.state in {"open", "waiting_inventory"}:
        engine.dispatch(
            engine.context.commands.create(
                "allocate",
                target=demand,
                correlation_id=flow_correlation_id(),
                key=("mro", demand.id, "allocate"),
            )
        )
        demand = persistence.entity("part_demand", entities.part_demand_id)
    if demand is not None and demand.state == "allocated":
        engine.dispatch(
            engine.context.commands.create(
                "consume",
                target=demand,
                correlation_id=flow_correlation_id(),
                key=("mro", demand.id, "consume"),
            )
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
    if not already_issued and not _can_issue_part_now(persistence, quantity=quantity):
        _dispatch_material_wait(engine, wo=wo, demand=demand)
        return False

    if not already_issued:
        _ensure_part_issue_requests(
            persistence,
            engine,
            backend,
            quantity=quantity,
        )
    backend.run_until(backend.now)

    if not _part_issue_now_complete(persistence, already_issued=already_issued):
        return False

    _dispatch_part_demand_progress(
        persistence,
        engine,
        entities=entities,
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
        backend,
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


def reconcile_scenario_emergency(
    persistence: MemoryPersistence,
    engine: Engine,
    backend: SimPyBackend,
    *,
    entities: MROEntities,
) -> bool:
    """Apply or unwind scenario-owned emergency pressure durably."""

    prefix = f"bay-emergency-scenario:{entities.work_order_id}:"
    active = engine.context.scenarios.attribute("mro.asset.emergency", False)

    if active:
        return reconcile_emergency_interrupt(
            persistence,
            engine,
            backend,
            entities=entities,
            request_prefix=prefix,
        )

    emergency_id = _active_emergency_request_id(
        persistence,
        entities.work_order_id,
        prefix=prefix,
    )
    committed = _committed_emergency_result(
        persistence,
        entities.work_order_id,
        prefix=prefix,
    )

    work_order = persistence.entity("work_order", entities.work_order_id)
    if work_order is None:
        raise RuntimeError("work order was not persisted")

    normal_id = f"bay:{entities.work_order_id}"
    normal_pending = engine.preemptive_resources.has_request(normal_id)

    # After the emergency reservation is released, cleanup may still be
    # incomplete while the interrupted work waits to reacquire its normal bay.
    # That normal request becomes the durable retry marker. Historical
    # preemption results alone are not sufficient.
    if emergency_id is None and not (
        work_order.state == "interrupted" and normal_pending
    ):
        return False

    if work_order.state == "in_progress" and committed is not None:
        reconcile_emergency_interrupt(
            persistence,
            engine,
            backend,
            entities=entities,
            request_prefix=prefix,
        )
        work_order = persistence.entity("work_order", entities.work_order_id)

    if work_order is not None and work_order.state == "interrupted":
        return reconcile_emergency_resume(
            persistence,
            engine,
            backend,
            entities=entities,
            request_prefix=prefix,
        )
    return False


def _active_emergency_request_id(
    persistence: MemoryPersistence,
    work_order_id: str,
    *,
    prefix: str | None = None,
) -> str | None:
    prefix = prefix or f"bay-emergency:{work_order_id}:"
    for demand in persistence.preemptive_resource_demands():
        if demand.request_id.startswith(prefix):
            return demand.request_id
    for reservation in persistence.preemptive_resource_reservations():
        if reservation.request_id.startswith(prefix):
            return reservation.request_id
    return None


def _committed_emergency_result(
    persistence: MemoryPersistence,
    work_order_id: str,
    *,
    prefix: str | None = None,
):
    normal_id = f"bay:{work_order_id}"
    prefix = prefix or f"bay-emergency:{work_order_id}:"
    matches = [
        result
        for result in persistence.resource_preemption_results()
        if result.displaced_request_id == normal_id
        and result.preempting_request_id.startswith(prefix)
    ]
    if not matches:
        return None

    active_request_id = _active_emergency_request_id(
        persistence,
        work_order_id,
        prefix=prefix,
    )
    if active_request_id is not None:
        active_matches = [
            result
            for result in matches
            if result.preempting_request_id == active_request_id
        ]
        if active_matches:
            return max(active_matches, key=lambda result: result.sequence)

    return max(matches, key=lambda result: result.sequence)


def _next_emergency_request_id(
    persistence: MemoryPersistence,
    work_order_id: str,
    *,
    prefix: str | None = None,
) -> str:
    prefix = prefix or f"bay-emergency:{work_order_id}:"
    active = _active_emergency_request_id(
        persistence, work_order_id, prefix=prefix
    )
    if active is not None:
        return active
    occurrence = 1 + sum(
        result.preempting_request_id.startswith(prefix)
        for result in persistence.resource_preemption_results()
    )
    return f"{prefix}{occurrence}"


def reconcile_emergency_interrupt(
    persistence: MemoryPersistence,
    engine: Engine,
    backend: SimPyBackend,
    *,
    entities: MROEntities,
    request_prefix: str | None = None,
) -> bool:
    """Preempt an active bay or reconcile an already-committed preemption."""

    wo = persistence.entity("work_order", entities.work_order_id)
    if wo is None:
        raise RuntimeError("work order was not persisted")
    if wo.state != "in_progress":
        return wo.state == "interrupted"

    normal_id = f"bay:{entities.work_order_id}"
    normal = engine.preemptive_resources.reservation_for(normal_id)
    if normal is None:
        return _reconcile_committed_emergency_interrupt(
            persistence,
            engine,
            wo=wo,
            work_order_id=entities.work_order_id,
            request_prefix=request_prefix,
        )

    emergency_id = _next_emergency_request_id(
        persistence,
        entities.work_order_id,
        prefix=request_prefix,
    )
    emergency = engine.preemptive_resources.ensure_requested(
        backend,
        resource_name="maintenance_bay",
        request_id=emergency_id,
        requested_at=backend.now,
        priority=1,
        preempt=True,
    )
    if emergency is None:
        return False

    displaced = next(
        (
            result
            for result in persistence.resource_preemption_results()
            if result.preempting_request_id == emergency_id
            and result.displaced_request_id == normal_id
        ),
        None,
    )
    if displaced is None:
        engine.preemptive_resources.withdraw(backend, emergency_id)
        return False

    _dispatch_work_order_interrupt(
        engine,
        wo=persistence.entity("work_order", entities.work_order_id),
        emergency_id=emergency_id,
    )
    return True


def _available_spare_parts(
    persistence: MemoryPersistence,
    engine: Engine,
) -> tuple[bool, float]:
    scenario_available = engine.context.scenarios.attribute(
        "mro.spare_parts.available", True
    )
    if not scenario_available:
        return False, 0.0
    lot_available = any(item.item_id == "part-lot-1" for item in persistence.store_items())
    available = next(
        (s.level for s in persistence.container_states() if s.name == "spare_parts"),
        0.0,
    )
    return lot_available, available


def _material_is_available(lot_available: bool, available: float, quantity: float) -> bool:
    return lot_available and available >= quantity


def _reconcile_material_shortage(
    persistence: MemoryPersistence,
    engine: Engine,
    backend: SimPyBackend,
    *,
    entities: MROEntities,
    wo,
    demand,
) -> None:
    if wo.state == "waiting_resource":
        release_capacity(
            persistence,
            engine,
            backend,
            entities=entities,
        )
        wo = persistence.entity("work_order", entities.work_order_id)
    if wo is not None and wo.state in {"released", "waiting_resource"}:
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


def _part_issue_now_complete(
    persistence: MemoryPersistence,
    *,
    already_issued: bool,
) -> bool:
    if already_issued:
        return True
    lot_done = any(
        r.request_id == "consume-part-lot-1"
        for r in persistence.store_get_results()
    )
    qty_done = any(
        r.request_id == "consume-spare-part-1"
        for r in persistence.container_operation_results()
    )
    return lot_done and qty_done


def _reconcile_committed_emergency_interrupt(
    persistence: MemoryPersistence,
    engine: Engine,
    *,
    wo,
    work_order_id: str,
    request_prefix: str | None,
) -> bool:
    active_emergency_id = _active_emergency_request_id(
        persistence,
        work_order_id,
        prefix=request_prefix,
    )
    if active_emergency_id is None:
        return False
    committed = _committed_emergency_result(
        persistence,
        work_order_id,
        prefix=request_prefix,
    )
    if committed is None or committed.preempting_request_id != active_emergency_id:
        return False
    _dispatch_work_order_interrupt(
        engine,
        wo=wo,
        emergency_id=committed.preempting_request_id,
    )
    return True


def _dispatch_work_order_interrupt(
    engine: Engine,
    *,
    wo,
    emergency_id: str,
) -> None:
    if wo is None or wo.state != "in_progress":
        return
    engine.dispatch(
        engine.context.commands.create(
            "interrupt",
            target=wo,
            correlation_id=flow_correlation_id(),
            key=("mro-emergency", wo.id, emergency_id, "interrupt"),
        )
    )


def reconcile_emergency_resume(
    persistence: MemoryPersistence,
    engine: Engine,
    backend: SimPyBackend,
    *,
    entities: MROEntities,
    request_prefix: str | None = None,
) -> bool:
    """Release emergency capacity and reacquire the bay only for interrupted work."""

    wo = persistence.entity("work_order", entities.work_order_id)
    if wo is None:
        raise RuntimeError("work order was not persisted")
    if wo.state == "in_progress":
        return True
    if wo.state != "interrupted":
        return False

    emergency_id = _active_emergency_request_id(
        persistence,
        entities.work_order_id,
        prefix=request_prefix,
    )
    normal_id = f"bay:{entities.work_order_id}"

    if emergency_id is not None:
        engine.preemptive_resources.withdraw(backend, emergency_id)

    normal = engine.preemptive_resources.ensure_requested(
        backend,
        resource_name="maintenance_bay",
        request_id=normal_id,
        requested_at=backend.now,
        priority=100,
        preempt=False,
    )
    if normal is None:
        return False

    wo = persistence.entity("work_order", entities.work_order_id)
    if wo is not None and wo.state == "interrupted":
        engine.dispatch(
            engine.context.commands.create(
                "resume",
                target=wo,
                correlation_id=flow_correlation_id(),
                key=("mro-emergency", wo.id, emergency_id or "none", "resume"),
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
    engine.resources.withdraw(
        backend,
        f"technician:{entities.work_order_id}",
    )
    engine.preemptive_resources.withdraw(
        backend,
        f"bay:{entities.work_order_id}",
    )


def _part_issue_started(persistence: MemoryPersistence) -> bool:
    return (
        any(
            request.request_id == "consume-part-lot-1"
            for request in persistence.store_get_requests()
        )
        or any(
            result.request_id == "consume-part-lot-1"
            for result in persistence.store_get_results()
        )
        or any(
            intent.request_id == "consume-spare-part-1"
            for intent in persistence.container_operation_intents()
        )
        or any(
            result.request_id == "consume-spare-part-1"
            for result in persistence.container_operation_results()
        )
    )


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
    if _part_issue_started(persistence):
        raise RuntimeError(
            "work order cannot be cancelled after spare-part issue has started"
        )

    if wo.state == "planned":
        engine.scheduler.cancel_pending(
            entity_type="work_order",
            entity_id=wo.id,
            name="release",
        )

    if wo.state in {"planned", "released", "waiting_material", "waiting_resource"}:
        engine.dispatch(
            engine.context.commands.create(
                "cancel",
                target=wo,
                correlation_id=flow_correlation_id(),
                key=("mro", wo.id, "cancel"),
            )
        )

    tech_request_id = f"technician:{entities.work_order_id}"
    bay_request_id = f"bay:{entities.work_order_id}"
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

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING
from datetime import datetime, timedelta

from sose.core.engine import Engine
from sose.core.randomness import RandomSource  # noqa: F401
from sose.core.runtime import (
    ContainerDefinition,
    PreemptiveResourceDefinition,
    ResourceDefinition,
    StoreDefinition,
)
from sose.persistence.memory import MemoryPersistence

from .entities import PartDemand, WorkOrder

if TYPE_CHECKING:
    from sose.backends.simpy import SimPyBackend
from .simulation_runtime import ORIGIN, PART_SKU, PART_CAPACITY, flow_correlation_id, build_runtime
from .simulation_capacity import reconcile_capacity, release_capacity
from .simulation_parts import (
    seed_spare_parts,
    reconcile_material_availability,
    reconcile_part_issue,
)
from .simulation_emergency import (
    _active_emergency_request_id,
    _committed_emergency_result,
    _next_emergency_request_id,
    _reconcile_committed_emergency_interrupt,
    _dispatch_work_order_interrupt,
    reconcile_emergency_resume,
)
from .simulation_closeout import reconcile_complete


@dataclass(frozen=True, slots=True)
class MROEntities:
    work_order_id: str
    part_demand_id: str


def seed_reference(
    persistence: MemoryPersistence,
    *,
    owner_id: str,
    quantity: float = 1.0,
    now: datetime = ORIGIN,
    technician_capacity: int = 1,
    maintenance_bay_capacity: int = 1,
    spare_part_store_capacity: int = 10,
    release_delay: timedelta = timedelta(hours=1),
) -> MROEntities:
    if quantity <= 0 or quantity > PART_CAPACITY:
        raise ValueError("quantity must fit spare-parts capacity")

    context, engine = build_runtime(persistence, owner_id=owner_id, now=now)
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


def build_demo(*, owner_id: str) -> tuple[Engine, MemoryPersistence, str]:
    persistence = MemoryPersistence()
    ids = seed_reference(persistence, owner_id=owner_id)
    _, engine = build_runtime(persistence, owner_id=owner_id)
    return engine, persistence, ids.work_order_id


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


def run_happy_path(
    *,
    owner_id: str,
    quantity: float = 1.0,
) -> tuple[MemoryPersistence, MROEntities]:
    from sose.backends.simpy import SimPyBackend

    persistence = MemoryPersistence()
    ids = seed_reference(persistence, owner_id=owner_id, quantity=quantity)
    _, engine = build_runtime(persistence, owner_id=owner_id)
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

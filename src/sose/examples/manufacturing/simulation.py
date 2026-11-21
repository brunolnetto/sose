from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from sose.backends.simpy import SimPyBackend
from sose.core.runtime import (
    ContainerDefinition,
    PreemptiveResourceDefinition,
    ResourceDefinition,
    StoreDefinition,
)
from sose.persistence.memory import MemoryPersistence

from .entities import Operation, ProductionOrder
from .simulation_runtime import (
    ORIGIN,
    SKU,
    MATERIAL_CAPACITY,
    FINISHED_CAPACITY,
    flow_correlation_id,
    build_runtime,
    _validate_quantity,
)
from .simulation_resources import (
    reconcile_setup_resources,
    release_setup_resources,
    reconcile_scenario_breakdown,
    reconcile_repair,
)
from .simulation_material import (
    seed_material,
    reconcile_material_availability,
    reconcile_material_issue,
)
from .simulation_output import (
    reconcile_wip_output,
    reconcile_quality_pass,
    reconcile_quality_hold,
    reconcile_output,
)

__all__ = [
    "ManufacturingEntities", "ORIGIN", "build_runtime", "flow_correlation_id",
    "reconcile_material_availability", "reconcile_material_issue", "reconcile_output",
    "reconcile_quality_hold", "reconcile_quality_pass", "reconcile_repair",
    "reconcile_scenario_breakdown", "reconcile_setup_resources", "reconcile_wip_output",
    "release_setup_resources", "run_happy_path", "seed_happy_path", "seed_material",
]


@dataclass(frozen=True, slots=True)
class ManufacturingEntities:
    production_order_id: str
    operation_id: str


def seed_happy_path(
    persistence: MemoryPersistence,
    *,
    owner_id: str,
    now: datetime = ORIGIN,
    quantity: float = 10.0,
) -> ManufacturingEntities:
    _validate_quantity(quantity)
    context, engine = build_runtime(persistence, owner_id=owner_id, now=now)
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
            due_at=now + offset,
            caused_by=previous,
            correlation_id=correlation_id,
            key=("manufacturing-reference", entity.entity_type, entity.id, trigger),
        )
        context.schedules.at(command.due_at, command=command)
        previous = command

    return ManufacturingEntities(order.id, operation.id)


def run_happy_path(
    *,
    owner_id: str,
    quantity: float = 10.0,
) -> tuple[MemoryPersistence, ManufacturingEntities]:
    persistence = MemoryPersistence()
    entities = seed_happy_path(persistence, owner_id=owner_id, quantity=quantity)
    _, engine = build_runtime(persistence, owner_id=owner_id)
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

"""Procure-to-pay constants, runtime setup and seeding (split from simulation.py)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sose.core.clock import SimulationClock
from sose.core.context import SimulationContext
from sose.core.engine import Engine
from sose.core.identity import deterministic_id
from sose.core.randomness import RandomSource
from sose.core.runtime import ContainerDefinition, ResourceDefinition, StoreDefinition
from sose.core.scheduler import Scheduler
from sose.domain.registry import DomainRegistry, EntityType
from sose.persistence.memory import MemoryPersistence

from .entities import MaterialDemand, PurchaseOrder, Receipt, Requisition
from .statecharts import (
    MaterialDemandChart,
    PurchaseOrderChart,
    ReceiptChart,
    RequisitionChart,
)


ORIGIN = datetime(2026, 1, 1, 8, tzinfo=timezone.utc)
SKU = "bearing-6204"
INVENTORY_CAPACITY = 1_000.0


@dataclass(frozen=True, slots=True)
class P2PEntities:
    requisition_id: str
    purchase_order_id: str
    receipt_id: str
    material_demand_id: str


def flow_correlation_id() -> str:
    return deterministic_id("p2p-flow", "p2p-happy", "req-1")


def build_runtime(
    persistence: MemoryPersistence,
    *,
    owner_id: str,
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
    registry.register(EntityType("requisition", RequisitionChart))
    registry.register(EntityType("purchase_order", PurchaseOrderChart))
    registry.register(EntityType("receipt", ReceiptChart))
    registry.register(EntityType("material_demand", MaterialDemandChart))
    return context, Engine(
        context=context,
        registry=registry,
        owner_id=owner_id,
        persistence=persistence,
        scenarios=scenarios,
    )


def _validate_quantity(quantity: float) -> None:
    if quantity <= 0 or quantity > INVENTORY_CAPACITY:
        raise ValueError(
            f"quantity must be > 0 and <= inventory capacity ({INVENTORY_CAPACITY})"
        )


def schedule_procurement_cycle(
    persistence: MemoryPersistence,
    engine: Engine,
    *,
    entities: P2PEntities,
    start_at: datetime,
    correlation_id: str | None = None,
    caused_by=None,
) -> tuple[datetime, ...]:
    """Schedule the canonical P2P procurement lifecycle with external causality.

    Standalone callers use the same schedule with the native P2P correlation. Composed
    callers may bind the first command to a durable cross-domain intent while all
    subsequent commands retain the ordinary deterministic command chain.
    """

    requisition = persistence.entity("requisition", entities.requisition_id)
    purchase_order = persistence.entity("purchase_order", entities.purchase_order_id)
    if requisition is None or purchase_order is None:
        raise RuntimeError("P2P procurement entities were not persisted")

    correlation = correlation_id or flow_correlation_id()
    schedule = (
        (requisition, "approve", timedelta(hours=1)),
        (requisition, "order", timedelta(hours=2)),
        (purchase_order, "submit", timedelta(hours=2)),
        (purchase_order, "confirm", timedelta(hours=3)),
        (purchase_order, "dispatch", timedelta(hours=4)),
        # The gap between dispatch and receive is the durable supplier lead time.
        (purchase_order, "receive", timedelta(hours=10)),
        (purchase_order, "close", timedelta(hours=11)),
    )
    previous = caused_by
    due_times: list[datetime] = []
    for entity, trigger, offset in schedule:
        command = engine.context.commands.create(
            trigger,
            target=entity,
            due_at=start_at + offset,
            caused_by=previous,
            correlation_id=correlation,
            key=("p2p-happy", entity.entity_type, entity.id, trigger),
        )
        engine.context.schedules.at(command.due_at, command=command)
        previous = command
        due_times.append(command.due_at)
    return tuple(due_times)


def seed_happy_path(
    persistence: MemoryPersistence,
    *,
    owner_id: str,
    now: datetime = ORIGIN,
    quantity: float = 10.0,
    schedule: bool = True,
) -> P2PEntities:
    _validate_quantity(quantity)
    context, engine = build_runtime(persistence, owner_id=owner_id, now=now)
    correlation_id = flow_correlation_id()

    requisition = context.entities.create(
        Requisition,
        key=("p2p-happy", "req-1"),
        state="requested",
        attributes={"sku": SKU, "quantity": quantity},
    )
    purchase_order = context.entities.create(
        PurchaseOrder,
        key=("p2p-happy", "po-1"),
        state="created",
        attributes={
            "sku": SKU,
            "quantity": quantity,
            "supplier": "supplier-a",
        },
    )
    receipt = context.entities.create(
        Receipt,
        key=("p2p-happy", "receipt-1"),
        state="pending",
        attributes={"sku": SKU, "quantity": quantity},
    )
    material_demand = context.entities.create(
        MaterialDemand,
        key=("p2p-happy", "demand-1"),
        state="open",
        attributes={"sku": SKU, "quantity": quantity},
    )

    with persistence.transaction() as uow:
        for entity in (requisition, purchase_order, receipt, material_demand):
            uow.save_entity(entity)
        uow.save_resource_definition(ResourceDefinition("receiving_dock", capacity=1))
        uow.save_resource_definition(ResourceDefinition("inspector", capacity=1))

    engine.stores.define(StoreDefinition("received_lots", kind="fifo", capacity=10))
    engine.containers.define(
        ContainerDefinition(
            "inventory",
            capacity=INVENTORY_CAPACITY,
            initial=0.0,
        )
    )

    seeded = P2PEntities(
        requisition_id=requisition.id,
        purchase_order_id=purchase_order.id,
        receipt_id=receipt.id,
        material_demand_id=material_demand.id,
    )
    if schedule:
        schedule_procurement_cycle(
            persistence,
            engine,
            entities=seeded,
            start_at=now,
            correlation_id=correlation_id,
        )

    return P2PEntities(
        requisition_id=requisition.id,
        purchase_order_id=purchase_order.id,
        receipt_id=receipt.id,
        material_demand_id=material_demand.id,
    )

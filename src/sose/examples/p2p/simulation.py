from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sose.backends.simpy import SimPyBackend
from sose.core.clock import SimulationClock
from sose.core.context import SimulationContext
from sose.core.engine import Engine
from sose.core.randomness import RandomSource
from sose.core.runtime import ContainerDefinition, StoreDefinition
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


@dataclass(frozen=True, slots=True)
class P2PEntities:
    requisition_id: str
    purchase_order_id: str
    receipt_id: str
    material_demand_id: str


def build_runtime(
    persistence: MemoryPersistence,
    *,
    now: datetime = ORIGIN,
) -> tuple[SimulationContext, Engine]:
    context = SimulationContext(
        clock=SimulationClock(now=now, step=timedelta(hours=1)),
        random=RandomSource(root_seed=42),
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
        persistence=persistence,
    )


def seed_happy_path(
    persistence: MemoryPersistence,
    *,
    quantity: float = 10.0,
) -> P2PEntities:
    context, engine = build_runtime(persistence)

    requisition = context.entities.create(
        Requisition,
        key=("p2p-happy", "req-1"),
        state="requested",
        attributes={"sku": "bearing-6204", "quantity": quantity},
    )
    purchase_order = context.entities.create(
        PurchaseOrder,
        key=("p2p-happy", "po-1"),
        state="created",
        attributes={
            "sku": "bearing-6204",
            "quantity": quantity,
            "supplier": "supplier-a",
        },
    )
    receipt = context.entities.create(
        Receipt,
        key=("p2p-happy", "receipt-1"),
        state="pending",
        attributes={"sku": "bearing-6204", "quantity": quantity},
    )
    material_demand = context.entities.create(
        MaterialDemand,
        key=("p2p-happy", "demand-1"),
        state="open",
        attributes={"sku": "bearing-6204", "quantity": quantity},
    )

    with persistence.transaction() as uow:
        for entity in (requisition, purchase_order, receipt, material_demand):
            uow.save_entity(entity)

    engine.stores.define(StoreDefinition("received_lots", kind="fifo", capacity=10))
    engine.containers.define(
        ContainerDefinition("inventory", capacity=1_000.0, initial=0.0)
    )

    schedule = (
        (requisition, "approve", timedelta(hours=1)),
        (requisition, "order", timedelta(hours=2)),
        (purchase_order, "submit", timedelta(hours=2)),
        (purchase_order, "confirm", timedelta(hours=3)),
        (purchase_order, "dispatch", timedelta(hours=4)),
        # The gap between dispatch and receive is the durable supplier lead time.
        (purchase_order, "receive", timedelta(hours=10)),
        (purchase_order, "close", timedelta(hours=11)),
        (receipt, "begin_receiving", timedelta(hours=10)),
        (receipt, "inspect", timedelta(hours=11)),
        (receipt, "stock", timedelta(hours=12)),
    )
    for entity, trigger, offset in schedule:
        command = context.commands.create(
            trigger,
            target=entity,
            due_at=ORIGIN + offset,
            key=("p2p-happy", entity.entity_type, entity.id, trigger),
        )
        context.schedules.at(command.due_at, command=command)

    return P2PEntities(
        requisition_id=requisition.id,
        purchase_order_id=purchase_order.id,
        receipt_id=receipt.id,
        material_demand_id=material_demand.id,
    )


def run_happy_path(
    *,
    quantity: float = 10.0,
) -> tuple[MemoryPersistence, P2PEntities]:
    persistence = MemoryPersistence()
    entities = seed_happy_path(persistence, quantity=quantity)
    _, engine = build_runtime(persistence)
    backend = SimPyBackend(origin=ORIGIN)

    engine.rebuild_backend(backend)
    backend.run_until(ORIGIN + timedelta(hours=12))

    engine.stores.put(
        backend,
        store_name="received_lots",
        item_id="receipt-lot-1",
        value={"sku": "bearing-6204", "quantity": quantity},
        requested_at=backend.now,
    )
    engine.containers.put(
        backend,
        container_name="inventory",
        request_id="stock-receipt-1",
        amount=quantity,
        requested_at=backend.now,
    )
    backend.run_until(backend.now)

    return persistence, entities

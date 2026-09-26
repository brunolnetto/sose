from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sose.backends.simpy import SimPyBackend
from sose.core.clock import SimulationClock
from sose.core.context import SimulationContext
from sose.core.engine import Engine
from sose.core.identity import deterministic_id
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


def _validate_quantity(quantity: float) -> None:
    if quantity <= 0 or quantity > INVENTORY_CAPACITY:
        raise ValueError(
            f"quantity must be > 0 and <= inventory capacity ({INVENTORY_CAPACITY})"
        )


def seed_happy_path(
    persistence: MemoryPersistence,
    *,
    quantity: float = 10.0,
) -> P2PEntities:
    _validate_quantity(quantity)
    context, engine = build_runtime(persistence)
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

    engine.stores.define(StoreDefinition("received_lots", kind="fifo", capacity=10))
    engine.containers.define(
        ContainerDefinition(
            "inventory",
            capacity=INVENTORY_CAPACITY,
            initial=0.0,
        )
    )

    schedule = (
        (requisition, "approve", timedelta(hours=1)),
        (requisition, "order", timedelta(hours=2)),
        (purchase_order, "submit", timedelta(hours=2)),
        (purchase_order, "confirm", timedelta(hours=3)),
        (purchase_order, "dispatch", timedelta(hours=4)),
        # The gap between dispatch and receive is the durable supplier lead time.
        (purchase_order, "receive", timedelta(hours=10)),
        (receipt, "begin_receiving", timedelta(hours=10)),
        (purchase_order, "close", timedelta(hours=11)),
        (receipt, "inspect", timedelta(hours=11)),
    )
    previous = None
    for entity, trigger, offset in schedule:
        command = context.commands.create(
            trigger,
            target=entity,
            due_at=ORIGIN + offset,
            caused_by=previous,
            correlation_id=correlation_id,
            key=("p2p-happy", entity.entity_type, entity.id, trigger),
        )
        context.schedules.at(command.due_at, command=command)
        previous = command

    return P2PEntities(
        requisition_id=requisition.id,
        purchase_order_id=purchase_order.id,
        receipt_id=receipt.id,
        material_demand_id=material_demand.id,
    )


def reconcile_stocking(
    persistence: MemoryPersistence,
    engine: Engine,
    backend: SimPyBackend,
    *,
    entities: P2PEntities,
    quantity: float,
) -> None:
    """Idempotently materialize inspected receipt inventory, then mark it stocked.

    The Store/Container records are durable recovery checkpoints. If a process stops
    after either submission or completion, rebuilding the backend plus rerunning this
    reconciler resumes from those records instead of repeating an already-committed
    effect.
    """

    _validate_quantity(quantity)
    correlation_id = flow_correlation_id()
    lot_id = "receipt-lot-1"
    stock_request_id = "stock-receipt-1"

    lot_exists = any(item.item_id == lot_id for item in persistence.store_items())
    lot_pending = any(
        intent.item_id == lot_id for intent in persistence.store_put_intents()
    )
    lot_consumed = any(
        result.item.item_id == lot_id for result in persistence.store_get_results()
    )
    if not (lot_exists or lot_pending or lot_consumed):
        engine.stores.put(
            backend,
            store_name="received_lots",
            item_id=lot_id,
            value={"sku": SKU, "quantity": quantity},
            requested_at=backend.now,
        )

    stock_done = any(
        result.request_id == stock_request_id
        for result in persistence.container_operation_results()
    )
    stock_pending = any(
        intent.request_id == stock_request_id
        for intent in persistence.container_operation_intents()
    )
    if not (stock_done or stock_pending):
        engine.containers.put(
            backend,
            container_name="inventory",
            request_id=stock_request_id,
            amount=quantity,
            requested_at=backend.now,
        )

    backend.run_until(backend.now)

    lot_durable = any(item.item_id == lot_id for item in persistence.store_items()) or any(
        result.item.item_id == lot_id for result in persistence.store_get_results()
    )
    stock_done = any(
        result.request_id == stock_request_id
        for result in persistence.container_operation_results()
    )
    if not lot_durable or not stock_done:
        raise RuntimeError("receipt inventory is not durably stocked yet")

    receipt = persistence.entity("receipt", entities.receipt_id)
    if receipt is None:  # pragma: no cover - seed invariant
        raise RuntimeError("receipt was not persisted")
    if receipt.state == "inspected":
        command = engine.context.commands.create(
            "stock",
            target=receipt,
            correlation_id=correlation_id,
            key=("p2p-happy", receipt.entity_type, receipt.id, "stock"),
        )
        engine.dispatch(command)
    elif receipt.state != "stocked":
        raise RuntimeError(f"receipt is not ready to stock: {receipt.state}")


def reconcile_consumption(
    persistence: MemoryPersistence,
    engine: Engine,
    backend: SimPyBackend,
    *,
    entities: P2PEntities,
    quantity: float,
) -> None:
    """Idempotently withdraw stocked inventory before consuming MaterialDemand."""

    _validate_quantity(quantity)
    correlation_id = flow_correlation_id()
    lot_request_id = "consume-receipt-lot-1"
    quantity_request_id = "consume-demand-1"

    if not any(
        request.request_id == lot_request_id
        for request in persistence.store_get_requests()
    ) and not any(
        result.request_id == lot_request_id
        for result in persistence.store_get_results()
    ):
        engine.stores.get(
            backend,
            store_name="received_lots",
            request_id=lot_request_id,
            requested_at=backend.now,
        )

    if not any(
        intent.request_id == quantity_request_id
        for intent in persistence.container_operation_intents()
    ) and not any(
        result.request_id == quantity_request_id
        for result in persistence.container_operation_results()
    ):
        engine.containers.get(
            backend,
            container_name="inventory",
            request_id=quantity_request_id,
            amount=quantity,
            requested_at=backend.now,
        )

    backend.run_until(backend.now)

    lot_done = any(
        result.request_id == lot_request_id
        for result in persistence.store_get_results()
    )
    quantity_done = any(
        result.request_id == quantity_request_id
        for result in persistence.container_operation_results()
    )
    if not lot_done or not quantity_done:
        raise RuntimeError("material demand inventory withdrawal is still pending")

    demand = persistence.entity("material_demand", entities.material_demand_id)
    if demand is None:  # pragma: no cover - seed invariant
        raise RuntimeError("material demand was not persisted")
    if demand.state in {"open", "waiting_inventory", "backordered"}:
        allocate = engine.context.commands.create(
            "allocate",
            target=demand,
            correlation_id=correlation_id,
            key=("p2p-happy", demand.entity_type, demand.id, "allocate"),
        )
        engine.dispatch(allocate)
        demand = persistence.entity("material_demand", entities.material_demand_id)

    if demand.state == "allocated":
        consume = engine.context.commands.create(
            "consume",
            target=demand,
            correlation_id=correlation_id,
            key=("p2p-happy", demand.entity_type, demand.id, "consume"),
        )
        engine.dispatch(consume)
    elif demand.state != "consumed":
        raise RuntimeError(f"material demand is not ready to consume: {demand.state}")


def run_happy_path(
    *,
    quantity: float = 10.0,
) -> tuple[MemoryPersistence, P2PEntities]:
    _validate_quantity(quantity)
    persistence = MemoryPersistence()
    entities = seed_happy_path(persistence, quantity=quantity)
    _, engine = build_runtime(persistence)
    backend = SimPyBackend(origin=ORIGIN)

    engine.rebuild_backend(backend)
    backend.run_until(ORIGIN + timedelta(hours=11))
    reconcile_stocking(
        persistence,
        engine,
        backend,
        entities=entities,
        quantity=quantity,
    )
    reconcile_consumption(
        persistence,
        engine,
        backend,
        entities=entities,
        quantity=quantity,
    )

    return persistence, entities

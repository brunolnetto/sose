from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sose.backends.simpy import SimPyBackend
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
    now: datetime = ORIGIN,
    quantity: float = 10.0,
) -> P2PEntities:
    _validate_quantity(quantity)
    context, engine = build_runtime(persistence, now=now)
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

    schedule_procurement_cycle(
        persistence,
        engine,
        entities=P2PEntities(
            requisition_id=requisition.id,
            purchase_order_id=purchase_order.id,
            receipt_id=receipt.id,
            material_demand_id=material_demand.id,
        ),
        start_at=now,
        correlation_id=correlation_id,
    )

    return P2PEntities(
        requisition_id=requisition.id,
        purchase_order_id=purchase_order.id,
        receipt_id=receipt.id,
        material_demand_id=material_demand.id,
    )


def request_receiving_slot(
    engine: Engine,
    backend: SimPyBackend,
    *,
    receipt_id: str,
    requested_at: datetime,
    priority: int = 100,
):
    return engine.resources.ensure_requested(
        backend,
        resource_name="receiving_dock",
        request_id=f"receiving-dock:{receipt_id}",
        requested_at=requested_at,
        priority=priority,
    )


def request_inspector(
    engine: Engine,
    backend: SimPyBackend,
    *,
    receipt_id: str,
    requested_at: datetime,
    priority: int = 100,
):
    return engine.resources.ensure_requested(
        backend,
        resource_name="inspector",
        request_id=f"inspector:{receipt_id}",
        requested_at=requested_at,
        priority=priority,
    )


def reconcile_receiving_resources(
    persistence: MemoryPersistence,
    engine: Engine,
    backend: SimPyBackend,
    *,
    entities: P2PEntities,
    outcome: str = "accepted",
) -> bool:
    """Advance receipt lifecycle only while required durable capacity is held.

    outcome controls the explicit business branch after dock acquisition:
    accepted -> inspection;
    partial -> mark partial, then inspection;
    rejected -> reject before inventory effects.
    """

    _validate_receiving_outcome(outcome)

    correlation_id = flow_correlation_id()
    dock_request_id = f"receiving-dock:{entities.receipt_id}"
    inspector_request_id = f"inspector:{entities.receipt_id}"

    receipt = _receipt_or_error(persistence, entities.receipt_id)
    if not _ensure_receiving_dock_if_pending(
        engine,
        backend,
        receipt=receipt,
        receipt_id=entities.receipt_id,
    ):
        return False

    receipt, completed = _apply_preinspection_receipt_transitions(
        persistence,
        engine,
        backend,
        receipt=receipt,
        receipt_id=entities.receipt_id,
        outcome=outcome,
        correlation_id=correlation_id,
        dock_request_id=dock_request_id,
    )
    if completed:
        return True

    if not _ensure_inspector_if_required(
        engine,
        backend,
        receipt=receipt,
        receipt_id=entities.receipt_id,
    ):
        return False

    if receipt.state in {"receiving", "partial"}:
        _dispatch_receipt_event(
            engine,
            receipt,
            event="inspect",
            key=("p2p-receiving", receipt.id, "inspect"),
            correlation_id=correlation_id,
        )
        receipt = _receipt_or_error(persistence, entities.receipt_id)
    return _finalize_receiving_result(
        engine,
        backend,
        receipt=receipt,
        inspector_request_id=inspector_request_id,
        dock_request_id=dock_request_id,
    )


def _validate_receiving_outcome(outcome: str) -> None:
    if outcome not in {"accepted", "partial", "rejected"}:
        raise ValueError(f"unsupported receipt outcome: {outcome}")


def _receipt_or_error(persistence: MemoryPersistence, receipt_id: str):
    receipt = persistence.entity("receipt", receipt_id)
    if receipt is None:
        raise RuntimeError("receipt was not persisted")
    return receipt


def _dispatch_receipt_event(
    engine: Engine,
    receipt,
    *,
    event: str,
    key: tuple[object, ...],
    correlation_id: str,
) -> None:
    command = engine.context.commands.create(
        event,
        target=receipt,
        correlation_id=correlation_id,
        key=key,
    )
    engine.dispatch(command)


def _ensure_receiving_dock_if_pending(
    engine: Engine,
    backend: SimPyBackend,
    *,
    receipt,
    receipt_id: str,
) -> bool:
    if receipt.state != "pending":
        return True
    dock = request_receiving_slot(
        engine,
        backend,
        receipt_id=receipt_id,
        requested_at=backend.now,
    )
    return dock is not None


def _ensure_inspector_if_required(
    engine: Engine,
    backend: SimPyBackend,
    *,
    receipt,
    receipt_id: str,
) -> bool:
    if receipt.state not in {"receiving", "partial"}:
        return True
    inspector = request_inspector(
        engine,
        backend,
        receipt_id=receipt_id,
        requested_at=backend.now,
    )
    return inspector is not None


def _apply_preinspection_receipt_transitions(
    persistence: MemoryPersistence,
    engine: Engine,
    backend: SimPyBackend,
    *,
    receipt,
    receipt_id: str,
    outcome: str,
    correlation_id: str,
    dock_request_id: str,
):
    if receipt.state == "pending":
        _dispatch_receipt_event(
            engine,
            receipt,
            event="begin_receiving",
            key=("p2p-receiving", receipt.id, "begin"),
            correlation_id=correlation_id,
        )
        receipt = _receipt_or_error(persistence, receipt_id)

    if receipt.state == "receiving" and outcome == "rejected":
        _dispatch_receipt_event(
            engine,
            receipt,
            event="reject",
            key=("p2p-receiving", receipt.id, "reject"),
            correlation_id=correlation_id,
        )
        engine.resources.withdraw(backend, dock_request_id)
        return _receipt_or_error(persistence, receipt_id), True

    if receipt.state == "receiving" and outcome == "partial":
        _dispatch_receipt_event(
            engine,
            receipt,
            event="mark_partial",
            key=("p2p-receiving", receipt.id, "partial"),
            correlation_id=correlation_id,
        )
        receipt = _receipt_or_error(persistence, receipt_id)
    return receipt, False


def _finalize_receiving_result(
    engine: Engine,
    backend: SimPyBackend,
    *,
    receipt,
    inspector_request_id: str,
    dock_request_id: str,
) -> bool:
    if receipt.state == "inspected":
        for request_id in (inspector_request_id, dock_request_id):
            engine.resources.withdraw(backend, request_id)
        return True
    return receipt.state == "stocked"


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

    _ensure_stocking_side_effects(
        persistence,
        engine,
        backend,
        lot_id=lot_id,
        stock_request_id=stock_request_id,
        quantity=quantity,
    )

    backend.run_until(backend.now)

    lot_durable = _lot_durable(persistence, lot_id)
    stock_done = _container_result_exists(persistence, stock_request_id)
    if not lot_durable or not stock_done:
        raise RuntimeError("receipt inventory is not durably stocked yet")

    receipt = persistence.entity("receipt", entities.receipt_id)
    if receipt is None:
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



def reconcile_shortage_state(
    persistence: MemoryPersistence,
    engine: Engine,
    *,
    entities: P2PEntities,
    required_quantity: float,
) -> bool:
    """Expose insufficient inventory as durable demand state before withdrawal.

    This guard deliberately runs before Store/Container GET operations so a
    partial receipt cannot consume the discrete lot while the quantitative
    withdrawal remains blocked.
    """

    _validate_quantity(required_quantity)
    available = next(
        (
            state.level
            for state in persistence.container_states()
            if state.name == "inventory"
        ),
        0.0,
    )
    if available >= required_quantity:
        return False

    demand = persistence.entity("material_demand", entities.material_demand_id)
    if demand is None:
        raise RuntimeError("material demand was not persisted")

    if demand.state == "open":
        wait = engine.context.commands.create(
            "wait_for_inventory",
            target=demand,
            correlation_id=flow_correlation_id(),
            key=("p2p-shortage", demand.id, "wait"),
        )
        engine.dispatch(wait)
        demand = persistence.entity("material_demand", entities.material_demand_id)

    if demand is not None and demand.state == "waiting_inventory":
        backorder = engine.context.commands.create(
            "backorder",
            target=demand,
            correlation_id=flow_correlation_id(),
            key=("p2p-shortage", demand.id, "backorder"),
        )
        engine.dispatch(backorder)

    return True

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

    _ensure_consumption_side_effects(
        persistence,
        engine,
        backend,
        lot_request_id=lot_request_id,
        quantity_request_id=quantity_request_id,
        quantity=quantity,
    )

    backend.run_until(backend.now)

    lot_done = _store_get_result_exists(persistence, lot_request_id)
    quantity_done = _container_result_exists(persistence, quantity_request_id)
    if not lot_done or not quantity_done:
        raise RuntimeError("material demand inventory withdrawal is still pending")

    demand = persistence.entity("material_demand", entities.material_demand_id)
    if demand is None:
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


def _store_get_request_exists(
    persistence: MemoryPersistence,
    request_id: str,
) -> bool:
    return any(
        request.request_id == request_id
        for request in persistence.store_get_requests()
    )


def _store_get_result_exists(
    persistence: MemoryPersistence,
    request_id: str,
) -> bool:
    return any(
        result.request_id == request_id
        for result in persistence.store_get_results()
    )


def _container_intent_exists(
    persistence: MemoryPersistence,
    request_id: str,
) -> bool:
    return any(
        intent.request_id == request_id
        for intent in persistence.container_operation_intents()
    )


def _container_result_exists(
    persistence: MemoryPersistence,
    request_id: str,
) -> bool:
    return any(
        result.request_id == request_id
        for result in persistence.container_operation_results()
    )


def _lot_visible_or_pending(
    persistence: MemoryPersistence,
    lot_id: str,
) -> bool:
    return any(item.item_id == lot_id for item in persistence.store_items()) or any(
        intent.item_id == lot_id for intent in persistence.store_put_intents()
    ) or any(
        result.item.item_id == lot_id for result in persistence.store_get_results()
    )


def _lot_durable(
    persistence: MemoryPersistence,
    lot_id: str,
) -> bool:
    return any(item.item_id == lot_id for item in persistence.store_items()) or any(
        result.item.item_id == lot_id for result in persistence.store_get_results()
    )


def _ensure_stocking_side_effects(
    persistence: MemoryPersistence,
    engine: Engine,
    backend: SimPyBackend,
    *,
    lot_id: str,
    stock_request_id: str,
    quantity: float,
) -> None:
    if not _lot_visible_or_pending(persistence, lot_id):
        engine.stores.put(
            backend,
            store_name="received_lots",
            item_id=lot_id,
            value={"sku": SKU, "quantity": quantity},
            requested_at=backend.now,
        )
    if _container_result_exists(persistence, stock_request_id) or _container_intent_exists(
        persistence,
        stock_request_id,
    ):
        return
    engine.containers.put(
        backend,
        container_name="inventory",
        request_id=stock_request_id,
        amount=quantity,
        requested_at=backend.now,
    )


def _ensure_consumption_side_effects(
    persistence: MemoryPersistence,
    engine: Engine,
    backend: SimPyBackend,
    *,
    lot_request_id: str,
    quantity_request_id: str,
    quantity: float,
) -> None:
    if not _store_get_request_exists(
        persistence,
        lot_request_id,
    ) and not _store_get_result_exists(
        persistence,
        lot_request_id,
    ):
        engine.stores.get(
            backend,
            store_name="received_lots",
            request_id=lot_request_id,
            requested_at=backend.now,
        )
    if _container_intent_exists(persistence, quantity_request_id) or _container_result_exists(
        persistence,
        quantity_request_id,
    ):
        return
    engine.containers.get(
        backend,
        container_name="inventory",
        request_id=quantity_request_id,
        amount=quantity,
        requested_at=backend.now,
    )


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
    backend.run_until(ORIGIN + timedelta(hours=10))
    if not reconcile_receiving_resources(
        persistence,
        engine,
        backend,
        entities=entities,
    ):
        raise RuntimeError("receipt capacity is still unavailable")
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


def run_shortage_backorder(
    *,
    quantity: float = 5.0,
) -> tuple[MemoryPersistence, P2PEntities]:
    """Exercise durable shortage waiting followed by exact-once replenishment."""

    _validate_quantity(quantity)
    persistence = MemoryPersistence()
    entities = seed_happy_path(persistence, quantity=quantity)
    context, engine = build_runtime(persistence)
    backend = SimPyBackend(origin=ORIGIN)
    engine.rebuild_backend(backend)

    demand = persistence.entity("material_demand", entities.material_demand_id)
    if demand is None:
        raise RuntimeError("material demand was not persisted")

    engine.stores.get(
        backend,
        store_name="received_lots",
        request_id="lot-for-demand-1",
        requested_at=ORIGIN,
    )
    engine.containers.get(
        backend,
        container_name="inventory",
        request_id="quantity-for-demand-1",
        amount=quantity,
        requested_at=ORIGIN,
    )
    backend.run_until(ORIGIN)

    for trigger in ("wait_for_inventory", "backorder"):
        current = persistence.entity("material_demand", demand.id)
        command = context.commands.create(
            trigger,
            target=current,
            correlation_id=flow_correlation_id(),
            key=("p2p-shortage", current.id, trigger),
        )
        engine.dispatch(command)

    engine.stores.put(
        backend,
        store_name="received_lots",
        item_id="replenishment-lot-1",
        value={"sku": SKU, "quantity": quantity},
        requested_at=ORIGIN,
    )
    engine.containers.put(
        backend,
        container_name="inventory",
        request_id="replenish-demand-1",
        amount=quantity,
        requested_at=ORIGIN,
    )
    backend.run_until(ORIGIN)

    lot_done = any(
        result.request_id == "lot-for-demand-1"
        for result in persistence.store_get_results()
    )
    quantity_done = any(
        result.request_id == "quantity-for-demand-1"
        for result in persistence.container_operation_results()
    )
    if not lot_done or not quantity_done:
        raise RuntimeError("backordered inventory is still pending")

    for trigger in ("allocate", "consume"):
        current = persistence.entity("material_demand", demand.id)
        command = context.commands.create(
            trigger,
            target=current,
            correlation_id=flow_correlation_id(),
            key=("p2p-shortage", current.id, trigger),
        )
        engine.dispatch(command)

    return persistence, entities

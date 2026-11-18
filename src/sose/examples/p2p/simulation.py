from __future__ import annotations

from datetime import timedelta  # noqa: F401

from sose.backends.simpy import SimPyBackend
from sose.core.clock import SimulationClock  # noqa: F401
from sose.core.context import SimulationContext  # noqa: F401
from sose.core.engine import Engine  # noqa: F401
from sose.core.identity import deterministic_id  # noqa: F401
from sose.core.randomness import RandomSource  # noqa: F401
from sose.core.runtime import ContainerDefinition, ResourceDefinition, StoreDefinition  # noqa: F401
from sose.core.scheduler import Scheduler  # noqa: F401
from sose.domain.registry import DomainRegistry, EntityType  # noqa: F401
from sose.persistence.memory import MemoryPersistence

from .entities import MaterialDemand, PurchaseOrder, Receipt, Requisition  # noqa: F401
from .statecharts import (  # noqa: F401
    MaterialDemandChart,
    PurchaseOrderChart,
    ReceiptChart,
    RequisitionChart,
)
from .foundation import (  # noqa: F401
    INVENTORY_CAPACITY,
    ORIGIN,
    P2PEntities,
    SKU,
    _validate_quantity,
    build_runtime,
    flow_correlation_id,
    schedule_procurement_cycle,
    seed_happy_path,
)
from .receiving import (  # noqa: F401
    reconcile_receiving_resources,
    request_inspector,
    request_receiving_slot,
)
from .inventory import (  # noqa: F401
    reconcile_consumption,
    reconcile_shortage_state,
    reconcile_stocking,
)


def run_happy_path(
    *,
    owner_id: str,
    quantity: float = 10.0,
) -> tuple[MemoryPersistence, P2PEntities]:
    _validate_quantity(quantity)
    persistence = MemoryPersistence()
    entities = seed_happy_path(persistence, owner_id=owner_id, quantity=quantity)
    _, engine = build_runtime(persistence, owner_id=owner_id)
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
    owner_id: str,
    quantity: float = 5.0,
) -> tuple[MemoryPersistence, P2PEntities]:
    """Exercise durable shortage waiting followed by exact-once replenishment."""

    _validate_quantity(quantity)
    persistence = MemoryPersistence()
    entities = seed_happy_path(persistence, owner_id=owner_id, quantity=quantity)
    context, engine = build_runtime(persistence, owner_id=owner_id)
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

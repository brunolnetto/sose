from __future__ import annotations

from dataclasses import dataclass

from sose.domain.entity import Entity


@dataclass(slots=True)
class FulfillmentOrder(Entity):
    entity_type: str = "warehouse_fulfillment_order"


@dataclass(slots=True)
class InventoryLot(Entity):
    entity_type: str = "warehouse_inventory_lot"


@dataclass(slots=True)
class Allocation(Entity):
    entity_type: str = "warehouse_allocation"


@dataclass(slots=True)
class InventoryOccurrence(Entity):
    entity_type: str = "warehouse_inventory_occurrence"

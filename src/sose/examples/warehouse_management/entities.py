from __future__ import annotations

from dataclasses import dataclass

from sose.domain.entity import Entity


@dataclass(slots=True)
class WarehouseSite(Entity):
    entity_type: str = "warehouse_management_site"


@dataclass(slots=True)
class Dock(Entity):
    entity_type: str = "warehouse_management_dock"


@dataclass(slots=True)
class Truck(Entity):
    entity_type: str = "warehouse_management_truck"


@dataclass(slots=True)
class Forklift(Entity):
    entity_type: str = "warehouse_management_forklift"


@dataclass(slots=True)
class StockPosition(Entity):
    entity_type: str = "warehouse_management_stock"


@dataclass(slots=True)
class Shipment(Entity):
    entity_type: str = "warehouse_management_shipment"

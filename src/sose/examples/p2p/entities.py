from __future__ import annotations

from dataclasses import dataclass

from sose.domain.entity import Entity


@dataclass(slots=True)
class Requisition(Entity):
    entity_type: str = "requisition"


@dataclass(slots=True)
class PurchaseOrder(Entity):
    entity_type: str = "purchase_order"


@dataclass(slots=True)
class Receipt(Entity):
    entity_type: str = "receipt"


@dataclass(slots=True)
class MaterialDemand(Entity):
    entity_type: str = "material_demand"

from __future__ import annotations

from dataclasses import dataclass

from sose.domain.entity import Entity


@dataclass(slots=True)
class SalesOrder(Entity):
    entity_type: str = "sales_order"


@dataclass(slots=True)
class Receivable(Entity):
    entity_type: str = "receivable"


@dataclass(slots=True)
class CollectionCase(Entity):
    entity_type: str = "collection_case"

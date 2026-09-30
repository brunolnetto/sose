"""Read-your-writes entity view over DomainWarehouse plus Engine OLTP outbox."""

from __future__ import annotations

from copy import deepcopy

from sose.domain.entity import Entity
from sose.domain.warehouse import DomainWarehouse
from sose.persistence.base import Persistence


class WarehouseBackedEntityStore:
    """Resolve domain state without requiring synchronous warehouse delivery.

    Pending DomainMutations are committed Engine OLTP facts.  They therefore
    override an older warehouse version until the outbox is delivered.
    """

    def __init__(self, persistence: Persistence, warehouse: DomainWarehouse) -> None:
        self.persistence = persistence
        self.warehouse = warehouse

    def entity(self, entity_type: str, entity_id: str) -> Entity | None:
        warehouse_entity = self.warehouse.entity(entity_type, entity_id)
        candidates = [warehouse_entity] if warehouse_entity is not None else []
        for delivery in self.persistence.domain_deliveries():
            entity = delivery.mutation.entity
            if entity.entity_type == entity_type and entity.id == entity_id:
                candidates.append(entity)
        if not candidates:
            return None
        max_version = max(item.version for item in candidates)
        latest = [item for item in candidates if item.version == max_version]
        if any(item != latest[0] for item in latest[1:]):
            raise RuntimeError(
                f"conflicting domain entity version: {entity_type}/{entity_id} v{max_version}"
            )
        return deepcopy(latest[0])

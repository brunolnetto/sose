from __future__ import annotations

from sose.domain.projector import DomainWarehouseProjector
from sose.domain.warehouse import DomainWarehouse
from sose.persistence.base import Persistence

from .simulation import MROEntities


def sync_mro_domain(
    persistence: Persistence,
    warehouse: DomainWarehouse,
    entities: MROEntities,
) -> int:
    """Publish the current MRO business state through the durable outbox."""

    return DomainWarehouseProjector(persistence, warehouse).sync_entities(
        (
            ("work_order", entities.work_order_id),
            ("part_demand", entities.part_demand_id),
        )
    )

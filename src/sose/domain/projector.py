"""Reference bridge that exports committed domain entities to a DomainWarehouse."""

from __future__ import annotations

from collections.abc import Iterable

from sose.core.identity import deterministic_id
from sose.domain.outbox import DomainMutationOutbox
from sose.domain.warehouse import DomainMutation, DomainWarehouse
from sose.persistence.base import Persistence


class DomainWarehouseProjector:
    """Project committed execution shadows through the durable domain outbox.

    Engine entities remain execution shadows until the kernel can read business
    state directly from DomainWarehouse.  The warehouse is nevertheless a
    physically independent, idempotently recoverable representation of domain
    truth for consumers.
    """

    def __init__(self, persistence: Persistence, warehouse: DomainWarehouse) -> None:
        self.persistence = persistence
        self.outbox = DomainMutationOutbox(persistence, warehouse)

    def prepare_entities(self, identities: Iterable[tuple[str, str]]) -> int:
        prepared = 0
        for entity_type, entity_id in identities:
            entity = self.persistence.entity(entity_type, entity_id)
            if entity is None:
                continue
            mutation = DomainMutation(
                mutation_id=deterministic_id(
                    "domain-entity-version",
                    entity.entity_type,
                    entity.id,
                    entity.version,
                ),
                entity=entity,
            )
            self.outbox.prepare(mutation)
            prepared += 1
        return prepared

    def sync_entities(self, identities: Iterable[tuple[str, str]]) -> int:
        self.prepare_entities(identities)
        return self.outbox.flush()

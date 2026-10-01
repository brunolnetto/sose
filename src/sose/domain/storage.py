"""Persistence facade that keeps business entities in DomainWarehouse.

Runtime resources, scheduling, checkpoints, commands and events remain in the
Engine Persistence. Entity reads are read-your-writes across the warehouse and
committed outbox; entity writes become DomainDelivery records in the current
Engine transaction.
"""
from __future__ import annotations

from contextlib import contextmanager
from copy import deepcopy

from sose.core.identity import deterministic_id
from sose.domain.delivery import DomainDelivery
from sose.domain.entity import Entity
from sose.domain.entity_store import WarehouseBackedEntityStore
from sose.domain.outbox import DomainMutationOutbox
from sose.domain.warehouse import DomainMutation, DomainWarehouse
from sose.persistence.base import Persistence


class DomainUnitOfWork:
    def __init__(self, inner, persistence: Persistence, warehouse: DomainWarehouse) -> None:
        self._inner = inner
        self._persistence = persistence
        self._warehouse = warehouse
        self._working: dict[tuple[str, str], Entity] = {}

    def __getattr__(self, name):
        return getattr(self._inner, name)

    def get_entity(self, entity_type: str, entity_id: str) -> Entity | None:
        key = (entity_type, entity_id)
        if key in self._working:
            return deepcopy(self._working[key])
        return WarehouseBackedEntityStore(
            self._persistence, self._warehouse
        ).entity(entity_type, entity_id)

    def save_entity(self, entity: Entity) -> None:
        mutation = DomainMutation(
            deterministic_id(
                "domain-entity-version",
                entity.entity_type,
                entity.id,
                entity.version,
            ),
            deepcopy(entity),
        )
        DomainMutationOutbox(self._persistence, self._warehouse).enqueue(
            self._inner, mutation
        )
        self._working[(entity.entity_type, entity.id)] = deepcopy(entity)


class DomainPersistence:
    """Domain-facing view over Engine Persistence and DomainWarehouse."""

    def __init__(self, engine: Persistence, warehouse: DomainWarehouse) -> None:
        self.engine = engine
        self.warehouse = warehouse
        self._entities = WarehouseBackedEntityStore(engine, warehouse)

    def __getattr__(self, name):
        return getattr(self.engine, name)

    def entity(self, entity_type: str, entity_id: str) -> Entity | None:
        return self._entities.entity(entity_type, entity_id)

    def entities(self) -> tuple[Entity, ...]:
        by_key = {
            (entity.entity_type, entity.id): entity
            for entity in self.warehouse.entities()
        }
        for delivery in self.engine.domain_deliveries():
            entity = delivery.mutation.entity
            key = (entity.entity_type, entity.id)
            current = by_key.get(key)
            if current is None or entity.version > current.version:
                by_key[key] = entity
            elif entity.version == current.version and entity != current:
                raise RuntimeError(
                    f"conflicting domain entity version: {entity.entity_type}/{entity.id} v{entity.version}"
                )
        return tuple(deepcopy(tuple(by_key.values())))

    @contextmanager
    def transaction(self):
        with self.engine.transaction() as inner:
            yield DomainUnitOfWork(inner, self.engine, self.warehouse)

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from sose.domain.config import DomainCatalog, DomainConfig, DomainDefinition
from sose.domain.delivery import DomainDelivery
from sose.domain.entity import Entity
from sose.domain.outbox import DomainMutationOutbox
from sose.domain.projector import DomainWarehouseProjector
from sose.domain.registry import DomainRegistry, EntityType
from sose.domain.warehouse import (
    DomainApplyResult,
    DomainMutation,
    MemoryDomainWarehouse,
)
from sose.persistence.memory import MemoryPersistence


NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)


class CoverageConfig(DomainConfig):
    start_at: datetime = NOW
    bootstrap_value: int = 1


def _definition(**overrides):
    values = dict(
        name="coverage-domain",
        description="coverage fixture",
        config_model=CoverageConfig,
        build_runtime=lambda *args: None,
        seed=lambda *args: None,
        runtime_mutable_fields=frozenset({"tick_step", "random_seed"}),
    )
    values.update(overrides)
    return DomainDefinition(**values)


def _entity(*, entity_id="e-1", state="new", version=0, value=1):
    return Entity(
        id=entity_id,
        entity_type="thing",
        state=state,
        attributes={"value": value},
        created_at=NOW,
        updated_at=NOW,
        version=version,
    )


def test_domain_definition_rejects_invalid_runtime_mutability():
    with pytest.raises(ValueError, match="runtime mutable fields are not in CoverageConfig"):
        _definition(runtime_mutable_fields=frozenset({"missing"}))

    with pytest.raises(ValueError, match="start_at cannot be runtime mutable"):
        _definition(runtime_mutable_fields=frozenset({"start_at"}))


def test_domain_definition_parsing_and_runtime_change_validation():
    definition = _definition()
    default = definition.parse_config()
    assert default == definition.default_config()
    assert definition.parse_config(default) is default

    changed = definition.parse_config(
        {
            **default.model_dump(),
            "random_seed": 99,
        }
    )
    assert definition.changed_config_fields(default, changed) == {"random_seed"}
    assert definition.validate_runtime_config_change(default, changed) == {"random_seed"}

    blocked = definition.parse_config(
        {
            **default.model_dump(),
            "bootstrap_value": 2,
        }
    )
    with pytest.raises(ValueError, match="bootstrap-only"):
        definition.validate_runtime_config_change(default, blocked)


def test_domain_definition_detects_schema_discovery_alignment_drift(monkeypatch):
    definition = _definition()
    monkeypatch.setattr(
        CoverageConfig,
        "model_json_schema",
        classmethod(lambda cls, **kwargs: {"properties": {}}),
    )

    with pytest.raises(RuntimeError, match="cannot align model fields"):
        definition.describe_config()


def test_domain_catalog_rejects_duplicates_and_unknown_names():
    catalog = DomainCatalog()
    definition = _definition()
    catalog.register(definition)

    assert catalog.get(definition.name) is definition
    assert catalog.names() == (definition.name,)
    assert catalog.definitions() == (definition,)

    with pytest.raises(ValueError, match="already registered"):
        catalog.register(definition)
    with pytest.raises(KeyError, match="unknown domain"):
        catalog.get("missing")


def test_domain_delivery_rejects_negative_attempts():
    mutation = DomainMutation("mutation-1", _entity())

    with pytest.raises(ValueError, match="attempts must be >= 0"):
        DomainDelivery(mutation, attempts=-1)


def test_domain_outbox_rejects_identity_conflicts_and_missing_delivery():
    persistence = MemoryPersistence()
    warehouse = MemoryDomainWarehouse()
    outbox = DomainMutationOutbox(persistence, warehouse)
    first = DomainMutation("same-id", _entity(value=1))
    conflicting = DomainMutation("same-id", _entity(value=2))

    assert outbox.prepare(first).mutation == first
    with pytest.raises(ValueError, match="domain mutation identity conflict"):
        outbox.prepare(conflicting)

    missing = DomainDelivery(DomainMutation("not-enqueued", _entity(entity_id="e-2")))
    assert outbox.deliver(missing) is None


def test_domain_outbox_failure_does_not_recreate_delivery_removed_concurrently():
    persistence = MemoryPersistence()
    mutation = DomainMutation("failing", _entity())

    class RemovingFailingWarehouse:
        def apply(self, current):
            with persistence.transaction() as uow:
                uow.delete_domain_delivery(current.mutation_id)
            raise RuntimeError("warehouse unavailable")

    outbox = DomainMutationOutbox(persistence, RemovingFailingWarehouse())
    delivery = outbox.prepare(mutation)

    with pytest.raises(RuntimeError, match="warehouse unavailable"):
        outbox.deliver(delivery)

    assert persistence.domain_delivery(mutation.mutation_id) is None


def test_domain_projector_skips_missing_entities():
    persistence = MemoryPersistence()
    projector = DomainWarehouseProjector(persistence, MemoryDomainWarehouse())

    assert projector.prepare_entities([("thing", "missing")]) == 0
    assert projector.sync_entities([("thing", "missing")]) == 0


def test_domain_registry_rejects_duplicate_entity_types():
    registry = DomainRegistry()
    definition = EntityType("thing", lambda **kwargs: kwargs)
    registry.register(definition)

    assert registry.get("thing") is definition
    entity = _entity()
    chart = registry.chart_for(entity, extra=1)
    assert chart["model"] == entity
    assert chart["extra"] == 1

    with pytest.raises(ValueError, match="already registered"):
        registry.register(definition)


def test_domain_mutation_requires_identity():
    with pytest.raises(ValueError, match="mutation_id cannot be empty"):
        DomainMutation("", _entity())


def test_memory_domain_warehouse_conflict_replay_and_supersede_semantics():
    warehouse = MemoryDomainWarehouse()
    original = _entity(version=2, value=1)
    first = DomainMutation("m-1", original)

    assert warehouse.apply(first) is DomainApplyResult.APPLIED
    assert warehouse.apply(first) is DomainApplyResult.REPLAYED

    with pytest.raises(ValueError, match="domain mutation identity conflict"):
        warehouse.apply(DomainMutation("m-1", _entity(version=2, value=2)))

    with pytest.raises(ValueError, match="conflicting domain entity version"):
        warehouse.apply(DomainMutation("m-2", _entity(version=2, value=2)))

    older = DomainMutation("m-old", _entity(version=1, value=0))
    assert warehouse.apply(older) is DomainApplyResult.SUPERSEDED
    assert warehouse.mutation("m-old") == older

    equivalent = DomainMutation("m-equal", _entity(version=2, value=1))
    assert warehouse.apply(equivalent) is DomainApplyResult.REPLAYED

    assert warehouse.entity("thing", "e-1") == original
    assert warehouse.entity("thing", "missing") is None
    assert warehouse.entities("thing") == (original,)
    assert warehouse.entities("other") == ()
    assert warehouse.mutation("missing") is None

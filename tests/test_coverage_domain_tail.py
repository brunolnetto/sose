from __future__ import annotations

import os
from uuid import uuid4

import pytest

from sose.domain.entity import Entity
from sose.domain.outbox import DomainMutationOutbox
from sose.domain.postgres import PostgresDomainWarehouse
from sose.domain.warehouse import DomainApplyResult, DomainMutation
from sose.persistence.memory import MemoryPersistence

DSN = os.environ.get("SOSE_TEST_POSTGRES_DSN")


def _entity(entity_id: str, version: int, state: str = "queued") -> Entity:
    return Entity(
        id=entity_id,
        entity_type="order",
        state=state,
        version=version,
    )


def test_domain_outbox_rejects_mutation_identity_conflict():
    persistence = MemoryPersistence()
    warehouse = object()
    outbox = DomainMutationOutbox(persistence, warehouse)  # type: ignore[arg-type]

    first = DomainMutation("same-id", _entity("1", 1))
    conflicting = DomainMutation("same-id", _entity("1", 2, "running"))

    with persistence.transaction() as uow:
        outbox.enqueue(uow, first)

    with persistence.transaction() as uow:
        with pytest.raises(ValueError, match="domain mutation identity conflict"):
            outbox.enqueue(uow, conflicting)


def test_postgres_domain_warehouse_validates_constructor_inputs():
    with pytest.raises(ValueError, match="dsn cannot be empty"):
        PostgresDomainWarehouse("")
    with pytest.raises(ValueError, match="namespace must be SQL-safe"):
        PostgresDomainWarehouse("postgresql://unused", namespace="bad-name")


@pytest.mark.skipif(not DSN, reason="SOSE_TEST_POSTGRES_DSN is required")
def test_postgres_domain_warehouse_covers_lookup_replay_and_version_edges():
    assert DSN is not None
    namespace = f"domain_tail_{uuid4().hex[:16]}"
    warehouse = PostgresDomainWarehouse(DSN, namespace=namespace)
    try:
        assert warehouse.entity("order", "missing") is None
        assert warehouse.entities() == ()
        assert warehouse.entities("order") == ()

        v1 = DomainMutation("m1", _entity("1", 1))
        assert warehouse.apply(v1) is DomainApplyResult.APPLIED
        assert warehouse.apply(v1) is DomainApplyResult.REPLAYED

        with pytest.raises(ValueError, match="domain mutation identity conflict"):
            warehouse.apply(DomainMutation("m1", _entity("1", 2, "running")))

        same_version = DomainMutation("m-same-version", _entity("1", 1))
        assert warehouse.apply(same_version) is DomainApplyResult.REPLAYED

        with pytest.raises(ValueError, match="conflicting domain entity version"):
            warehouse.apply(
                DomainMutation(
                    "m-conflict-version",
                    _entity("1", 1, "different-state"),
                )
            )

        assert (
            warehouse.apply(DomainMutation("m2", _entity("1", 2, "running")))
            is DomainApplyResult.APPLIED
        )
        assert (
            warehouse.apply(DomainMutation("m-old", _entity("1", 1)))
            is DomainApplyResult.SUPERSEDED
        )

        warehouse.apply(DomainMutation("m-other", _entity("2", 1, "queued")))

        assert warehouse.entity("order", "1") == _entity("1", 2, "running")
        assert [entity.id for entity in warehouse.entities()] == ["1", "2"]
        assert [entity.id for entity in warehouse.entities("order")] == ["1", "2"]
        assert warehouse.entities("other") == ()
    finally:
        warehouse.close()

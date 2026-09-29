from __future__ import annotations

import os
from threading import Barrier, Thread
from uuid import uuid4

import pytest

from sose.domain.entity import Entity
from sose.persistence.postgres import PostgresPersistence
from tests.support.persistence_conformance import PersistenceConformanceSuite


DSN = os.environ.get("SOSE_TEST_POSTGRES_DSN")

pytestmark = pytest.mark.skipif(
    not DSN,
    reason="SOSE_TEST_POSTGRES_DSN is required for PostgreSQL integration tests",
)


def _namespace(prefix: str = "test") -> str:
    return f"{prefix}_{uuid4().hex[:16]}"


class TestPostgresPersistenceConformance(PersistenceConformanceSuite):
    def make_persistence(self):
        assert DSN is not None
        return PostgresPersistence(DSN, namespace=_namespace("conf"))


def test_postgres_adapter_persists_backend_neutral_records():
    assert DSN is not None
    persistence = PostgresPersistence(DSN, namespace=_namespace("records"))
    entity = Entity(
        id="entity-1",
        entity_type="postgres_test",
        state="ready",
        attributes={"value": 1},
        version=1,
    )
    with persistence.transaction() as uow:
        uow.save_entity(entity)

    assert persistence.persisted_record_count() > 0
    assert persistence.entity("postgres_test", "entity-1") == entity
    persistence.close()


def test_postgres_independent_connections_commit_disjoint_records_concurrently():
    assert DSN is not None
    namespace = _namespace("concurrent")
    seed = PostgresPersistence(DSN, namespace=namespace)
    with seed.transaction() as uow:
        for ordinal in (1, 2):
            uow.save_entity(
                Entity(
                    id=f"entity-{ordinal}",
                    entity_type="postgres_test",
                    state="ready",
                    attributes={"worker": None},
                    version=1,
                )
            )
    seed.close()

    barrier = Barrier(2)
    errors: list[BaseException] = []

    def worker(entity_id: str, worker_name: str) -> None:
        persistence = PostgresPersistence(DSN, namespace=namespace)
        try:
            with persistence.transaction() as uow:
                entity = uow.get_entity("postgres_test", entity_id)
                assert entity is not None
                barrier.wait(timeout=10)
                entity.attributes["worker"] = worker_name
                entity.version += 1
                uow.save_entity(entity)
        except BaseException as exc:  # pragma: no cover - surfaced below
            errors.append(exc)
        finally:
            persistence.close()

    left = Thread(target=worker, args=("entity-1", "left"))
    right = Thread(target=worker, args=("entity-2", "right"))
    left.start()
    right.start()
    left.join(timeout=20)
    right.join(timeout=20)

    assert not left.is_alive()
    assert not right.is_alive()
    assert errors == []

    reopened = PostgresPersistence(DSN, namespace=namespace)
    first = reopened.entity("postgres_test", "entity-1")
    second = reopened.entity("postgres_test", "entity-2")
    assert first is not None and first.attributes["worker"] == "left"
    assert second is not None and second.attributes["worker"] == "right"
    reopened.close()


def test_postgres_revision_invalidates_stale_connection_cache():
    assert DSN is not None
    namespace = _namespace("revision")
    first = PostgresPersistence(DSN, namespace=namespace)
    second = PostgresPersistence(DSN, namespace=namespace)

    entity = Entity(
        id="entity-1",
        entity_type="postgres_test",
        state="ready",
        attributes={"value": 1},
        version=1,
    )
    with first.transaction() as uow:
        uow.save_entity(entity)

    observed = second.entity("postgres_test", "entity-1")
    assert observed == entity

    with first.transaction() as uow:
        updated = uow.get_entity("postgres_test", "entity-1")
        assert updated is not None
        updated.attributes["value"] = 2
        updated.version = 2
        uow.save_entity(updated)

    refreshed = second.entity("postgres_test", "entity-1")
    assert refreshed is not None
    assert refreshed.attributes["value"] == 2
    assert refreshed.version == 2

    first.close()
    second.close()

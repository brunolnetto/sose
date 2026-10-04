from copy import deepcopy
from datetime import datetime, timezone

from sose.domain.entity import Entity
from sose.persistence.memory import MemoryPersistence, MemoryUnitOfWork
from sose.persistence.records import changes_for_dirty_records


NOW = datetime(2026, 9, 28, 12, tzinfo=timezone.utc)


def test_unit_of_work_records_only_touched_semantic_identities():
    persistence = MemoryPersistence()
    before = deepcopy(persistence._state)
    uow = MemoryUnitOfWork(deepcopy(before), persistence)

    entity = Entity(
        id="one",
        entity_type="demo",
        state="planned",
        created_at=NOW,
        updated_at=NOW,
        version=1,
    )
    uow.save_entity(entity)
    uow.set_committed_tick(7)

    assert uow.dirty_records == frozenset(
        {
            ("entities", ("demo", "one")),
            ("committed_tick", "__scalar__"),
        }
    )

    changes = changes_for_dirty_records(
        before,
        uow._working,
        uow.dirty_records,
    )
    assert {(change.collection, change.operation) for change in changes} == {
        ("entities", "upsert"),
        ("committed_tick", "upsert"),
    }


def test_idempotent_save_can_be_marked_dirty_without_emitting_record_change():
    persistence = MemoryPersistence()
    entity = Entity(
        id="one",
        entity_type="demo",
        state="planned",
        created_at=NOW,
        updated_at=NOW,
        version=1,
    )
    with persistence.transaction() as uow:
        uow.save_entity(entity)

    before = deepcopy(persistence._state)
    uow = MemoryUnitOfWork(deepcopy(before), persistence)
    uow.save_entity(entity)

    assert ("entities", ("demo", "one")) in uow.dirty_records
    assert changes_for_dirty_records(
        before,
        uow._working,
        uow.dirty_records,
    ) == ()


def test_one_entity_update_encodes_one_record_in_large_state():
    persistence = MemoryPersistence()
    with persistence.transaction() as uow:
        for index in range(500):
            uow.save_entity(
                Entity(
                    id=f"entity-{index:04d}",
                    entity_type="demo",
                    state="planned",
                    created_at=NOW,
                    updated_at=NOW,
                    version=1,
                )
            )

    before = deepcopy(persistence._state)
    uow = MemoryUnitOfWork(deepcopy(before), persistence)
    target = uow.get_entity("demo", "entity-0250")
    assert target is not None
    target.state = "running"
    target.version += 1
    uow.save_entity(target)

    changes = changes_for_dirty_records(
        before,
        uow._working,
        uow.dirty_records,
    )

    assert len(changes) == 1
    assert changes[0].collection == "entities"
    assert changes[0].operation == "upsert"

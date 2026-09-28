from sose.domain.entity import Entity
from sose.persistence.memory import MemoryPersistence, fork_state


def _entity(value: int = 1) -> Entity:
    return Entity(
        id="entity-1",
        entity_type="cow",
        state="ready",
        attributes={"value": value},
        version=value,
    )


def test_fork_state_copies_collections_but_shares_untouched_records():
    persistence = MemoryPersistence()
    with persistence.transaction() as uow:
        uow.save_entity(_entity())

    original = persistence._state
    forked = fork_state(original)

    assert forked is not original
    assert forked.entities is not original.entities
    assert forked.entities[("cow", "entity-1")] is original.entities[
        ("cow", "entity-1")
    ]


def test_uow_get_returns_isolated_copy_under_structural_fork():
    persistence = MemoryPersistence()
    with persistence.transaction() as uow:
        uow.save_entity(_entity())

    with persistence.transaction() as uow:
        candidate = uow.get_entity("cow", "entity-1")
        assert candidate is not None
        candidate.attributes["value"] = 99
        # no save_entity(candidate)

    persisted = persistence.entity("cow", "entity-1")
    assert persisted is not None
    assert persisted.attributes["value"] == 1


def test_rollback_does_not_leak_saved_record_from_structural_fork():
    persistence = MemoryPersistence()
    with persistence.transaction() as uow:
        uow.save_entity(_entity())

    try:
        with persistence.transaction() as uow:
            candidate = uow.get_entity("cow", "entity-1")
            assert candidate is not None
            candidate.attributes["value"] = 2
            candidate.version = 2
            uow.save_entity(candidate)
            raise RuntimeError("rollback")
    except RuntimeError:
        pass

    persisted = persistence.entity("cow", "entity-1")
    assert persisted is not None
    assert persisted.attributes["value"] == 1
    assert persisted.version == 1


def test_committed_structural_fork_replaces_only_touched_record():
    persistence = MemoryPersistence()
    with persistence.transaction() as uow:
        uow.save_entity(_entity())
        uow.save_entity(
            Entity(
                id="entity-2",
                entity_type="cow",
                state="ready",
                attributes={"value": 2},
                version=2,
            )
        )

    untouched_before = persistence._state.entities[("cow", "entity-2")]

    with persistence.transaction() as uow:
        candidate = uow.get_entity("cow", "entity-1")
        assert candidate is not None
        candidate.attributes["value"] = 3
        candidate.version = 3
        uow.save_entity(candidate)

    untouched_after = persistence._state.entities[("cow", "entity-2")]
    assert untouched_after is untouched_before
    assert persistence.entity("cow", "entity-1").attributes["value"] == 3

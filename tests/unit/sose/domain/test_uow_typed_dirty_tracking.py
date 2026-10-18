"""#528: one-record dirty saves distinguish Python numeric/bool semantic types.

The tagged-JSON codec, not Python's bool/int/float equality, owns the durable
record representation. A semantic change requires a single authoritative
upsert while an identical save must remain a no-op.
"""
from __future__ import annotations

from copy import deepcopy
from contextlib import nullcontext

import pytest

from sose.domain.entity import Entity
from sose.persistence.codec import dumps
from sose.persistence.memory import MemoryPersistence, MemoryUnitOfWork, _State
from sose.persistence.records import changes_for_dirty_records
from sose.persistence.sqlite import SQLitePersistence
from sose.persistence.sqlite_incremental import SQLiteIncrementalPersistence
from sose.persistence.jsonl_journal import JSONLJournalPersistence


@pytest.mark.parametrize(
    ("original", "changed"),
    [
        (0, False),
        (0.0, False),
        (1, True),
        (1.0, True),
        (0, 0.0),
        (1, 1.0),
        (False, 0),
    ],
    ids=[
        "int-to-false", "float-to-false", "int-to-true",
        "float-to-true", "int-to-float", "positive-int-to-float",
        "false-to-int",
    ],
)
def test_typed_entity_record_dirty_tracking_rejects_python_equal_values(original, changed):
    store = MemoryPersistence()
    original_entity = Entity(
        id="target", entity_type="typed-dirty",
        state="ready", attributes={
            "nested": {"actions": [{"seq": original, "at": original}]}
        },
    )
    untouched = Entity(
        id="peer", entity_type="typed-dirty", state="independent",
        attributes={"immutable": "untouched"},
    )
    with store.transaction() as uow:
        uow.save_entity(original_entity)
        uow.save_entity(untouched)
    before = deepcopy(store._state)
    uow = MemoryUnitOfWork(deepcopy(before), store)
    updated = uow.get_entity("typed-dirty", "target")
    assert updated is not None
    updated.attributes["nested"]["actions"][0]["seq"] = changed
    updated.attributes["nested"]["actions"][0]["at"] = changed
    uow.save_entity(updated)

    assert original == changed  # Demonstrate the exact bug being falsified.
    changes = changes_for_dirty_records(before, uow._working, uow.dirty_records)
    assert len(changes) == 1, changes
    assert changes[0].collection == "entities"
    assert changes[0].operation == "upsert"
    assert changes[0].key == dumps(("typed-dirty", "target"))
    assert changes[0].payload == dumps(updated)
    assert changes[0].payload != dumps(original_entity)
    assert uow._working.entities[("typed-dirty", "peer")] == untouched

    # One complete UoW changes the in-memory source exactly once.
    uow.commit()
    saved = store.entity("typed-dirty", "target")
    assert saved is not None
    assert type(saved.attributes["nested"]["actions"][0]["seq"]) is type(changed)
    assert store.entity("typed-dirty", "peer") == untouched

    second = MemoryUnitOfWork(deepcopy(store._state), store)
    second_before = deepcopy(store._state)
    second.save_entity(saved)
    assert changes_for_dirty_records(
        second_before, second._working, second.dirty_records,
    ) == ()


@pytest.mark.parametrize(
    ("before", "after", "expected"),
    [
        (0, False, True),
        (0.0, False, True),
        (1, True, True),
        (1.0, True, True),
        (0, 0.0, True),
        (1, 1.0, True),
        (0, 0, False),
        (False, False, False),
    ],
)
def test_typed_list_and_scalar_dirty_collections(before, after, expected):
    state_before = _State()
    state_after = deepcopy(state_before)
    state_before.events = [before]
    state_after.events = [after]
    state_before.committed_ticks = {"org": before}
    state_after.committed_ticks = {"org": after}
    list_change = changes_for_dirty_records(
        state_before, state_after, {("events", 0)},
    )
    dict_change = changes_for_dirty_records(
        state_before, state_after, {("committed_ticks", "org")},
    )
    assert bool(list_change) is expected
    assert bool(dict_change) is expected
    if expected:
        assert list_change[0].payload == dumps(after)
        assert dict_change[0].payload == dumps(after)


@pytest.mark.parametrize(
    ("adapter", "filename"),
    [
        (SQLitePersistence, "snapshot.db"),
        (SQLiteIncrementalPersistence, "incremental.db"),
        (JSONLJournalPersistence, "journal.jsonl"),
    ],
)
@pytest.mark.parametrize("changed", (False, 0.0, True, 1.0))
def test_typed_nested_entity_update_survives_close_and_reopen(
    tmp_path, adapter, filename, changed,
):
    path = tmp_path / filename
    original = int(changed)
    if changed is False or changed == 0.0:
        original = 0
    else:
        original = 1
    assert original == changed

    with (nullcontext(adapter(path)) if adapter is JSONLJournalPersistence else adapter(path)) as store:
        with store.transaction() as uow:
            uow.save_entity(Entity(
                id="owner-entity", entity_type="typed-dirty",
                state="ready", attributes={"nested": {"sequence": original}},
            ))
            uow.save_entity(Entity(
                id="peer-entity", entity_type="typed-dirty",
                state="ready", attributes={"nested": {"sequence": "unchanged"}},
            ))
        with store.transaction() as uow:
            target = uow.get_entity("typed-dirty", "owner-entity")
            assert target is not None
            target.attributes["nested"]["sequence"] = changed
            uow.save_entity(target)

    with (nullcontext(adapter(path)) if adapter is JSONLJournalPersistence else adapter(path)) as reopened:
        saved = reopened.entity("typed-dirty", "owner-entity")
        assert saved is not None
        assert saved.attributes["nested"]["sequence"] == changed
        assert type(saved.attributes["nested"]["sequence"]) is type(changed)
        assert reopened.entity(
            "typed-dirty", "peer-entity",
        ).attributes["nested"]["sequence"] == "unchanged"
        with reopened.transaction() as uow:
            uow.save_entity(saved)
        assert reopened.entity("typed-dirty", "owner-entity") == saved


@pytest.mark.parametrize("type_only_change", (False, True))
def test_nested_mapping_reorder_is_idempotent_but_type_change_is_not(type_only_change):
    store = MemoryPersistence()
    original = Entity(
        id="reordered", entity_type="typed-dirty", state="ready",
        attributes={
            "work": {"a": 0, "b": {"one": 1.0, "two": 2}},
            "peer": [{"x": 1, "y": 2}],
        },
    )
    with store.transaction() as uow:
        uow.save_entity(original)
    before = deepcopy(store._state)
    target = deepcopy(original)
    target.attributes = {
        "peer": [{"y": 2, "x": 1}],
        "work": {
            "b": {"two": 2, "one": 1.0},
            "a": False if type_only_change else 0,
        },
    }
    # The original codec intentionally preserves dictionary insertion order;
    # its record bytes are not changed to satisfy this comparison contract.
    assert dumps(target) != dumps(original)
    uow = MemoryUnitOfWork(deepcopy(before), store)
    uow.save_entity(target)
    changes = changes_for_dirty_records(before, uow._working, uow.dirty_records)
    if type_only_change:
        assert len(changes) == 1
        assert changes[0].payload == dumps(target)
    else:
        assert changes == ()
    # A literal no-op must not demand a new backend revision or journal append.
    assert ("entities", ("typed-dirty", "reordered")) in uow.dirty_records

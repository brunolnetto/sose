from __future__ import annotations

from dataclasses import dataclass, fields
from typing import Iterable

from .codec import dumps, loads
from .memory import _State


@dataclass(frozen=True, slots=True)
class StateRecord:
    collection: str
    key: str
    position: int
    payload: str


@dataclass(frozen=True, slots=True)
class StateRecordChange:
    operation: str
    collection: str
    key: str
    position: int | None = None
    payload: str | None = None


def state_to_records(state: _State) -> dict[tuple[str, str], StateRecord]:
    """Project SOSE durable state into backend-neutral keyed records."""

    records: dict[tuple[str, str], StateRecord] = {}
    for field in fields(_State):
        name = field.name
        value = getattr(state, name)

        if isinstance(value, dict):
            for position, (key, item) in enumerate(value.items()):
                encoded_key = dumps(key)
                records[(name, encoded_key)] = StateRecord(
                    collection=name,
                    key=encoded_key,
                    position=position,
                    payload=dumps(item),
                )
            continue

        if isinstance(value, list):
            for position, item in enumerate(value):
                key = str(position)
                records[(name, key)] = StateRecord(
                    collection=name,
                    key=key,
                    position=position,
                    payload=dumps(item),
                )
            continue

        records[(name, "__scalar__")] = StateRecord(
            collection=name,
            key="__scalar__",
            position=0,
            payload=dumps(value),
        )

    return records


def records_to_state(records: Iterable[StateRecord]) -> _State:
    """Reconstruct SOSE durable state from backend-neutral keyed records."""

    grouped: dict[str, list[StateRecord]] = {}
    for record in records:
        grouped.setdefault(record.collection, []).append(record)

    template = _State()
    values: dict[str, object] = {}

    for field in fields(_State):
        name = field.name
        default_value = getattr(template, name)
        current = sorted(
            grouped.get(name, ()),
            key=lambda record: (record.position, record.key),
        )

        if isinstance(default_value, dict):
            restored: dict[object, object] = {}
            for record in current:
                restored[loads(record.key)] = loads(record.payload)
            values[name] = restored
            continue

        if isinstance(default_value, list):
            values[name] = [loads(record.payload) for record in current]
            continue

        scalar = next(
            (record for record in current if record.key == "__scalar__"),
            None,
        )
        values[name] = (
            default_value if scalar is None else loads(scalar.payload)
        )

    return _State(**values)


def diff_state_records(
    before: _State,
    after: _State,
) -> tuple[StateRecordChange, ...]:
    """Return deterministic upsert/delete operations between two states."""

    old = state_to_records(before)
    new = state_to_records(after)
    changes: list[StateRecordChange] = []

    for identity in sorted(old.keys() - new.keys()):
        collection, key = identity
        changes.append(
            StateRecordChange(
                operation="delete",
                collection=collection,
                key=key,
            )
        )

    for identity in sorted(new):
        record = new[identity]
        if old.get(identity) == record:
            continue
        changes.append(
            StateRecordChange(
                operation="upsert",
                collection=record.collection,
                key=record.key,
                position=record.position,
                payload=record.payload,
            )
        )

    return tuple(changes)


def changes_for_dirty_records(
    before: _State,
    after: _State,
    dirty_records: Iterable[tuple[str, object]],
) -> tuple[StateRecordChange, ...]:
    """Encode only record identities explicitly touched by a UnitOfWork."""

    template = _State()
    changes: list[StateRecordChange] = []

    for collection, raw_key in sorted(
        dirty_records,
        key=lambda item: (item[0], dumps(item[1])),
    ):
        before_value = getattr(before, collection)
        after_value = getattr(after, collection)

        if isinstance(after_value, dict):
            before_item = before_value.get(raw_key)
            exists_after = raw_key in after_value
            after_item = after_value.get(raw_key)
            if before_item == after_item and (raw_key in before_value) == exists_after:
                continue
            encoded_key = dumps(raw_key)
            if not exists_after:
                changes.append(
                    StateRecordChange(
                        operation="delete",
                        collection=collection,
                        key=encoded_key,
                    )
                )
                continue
            position = list(after_value).index(raw_key)
            changes.append(
                StateRecordChange(
                    operation="upsert",
                    collection=collection,
                    key=encoded_key,
                    position=position,
                    payload=dumps(after_item),
                )
            )
            continue

        if isinstance(after_value, list):
            index = int(raw_key)
            before_item = before_value[index] if index < len(before_value) else None
            after_item = after_value[index] if index < len(after_value) else None
            if before_item == after_item and index < len(before_value) == (index < len(after_value)):
                continue
            if index >= len(after_value):
                changes.append(
                    StateRecordChange(
                        operation="delete",
                        collection=collection,
                        key=str(index),
                    )
                )
                continue
            changes.append(
                StateRecordChange(
                    operation="upsert",
                    collection=collection,
                    key=str(index),
                    position=index,
                    payload=dumps(after_item),
                )
            )
            continue

        default_value = getattr(template, collection)
        if before_value == after_value:
            continue
        changes.append(
            StateRecordChange(
                operation="upsert",
                collection=collection,
                key="__scalar__",
                position=0,
                payload=dumps(after_value if after_value is not None else default_value),
            )
        )

    return tuple(changes)

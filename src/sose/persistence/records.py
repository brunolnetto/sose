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

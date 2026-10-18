from __future__ import annotations

from dataclasses import dataclass, fields
import json
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
    known = {field.name for field in fields(_State)}
    legacy = {"simulation_position", "scenario_state", "committed_tick"}
    unknown = set(grouped) - known
    if unknown:
        if unknown & legacy:
            raise ValueError(
                "unsupported legacy unowned runtime records: "
                + ", ".join(sorted(unknown & legacy))
            )
        raise ValueError(
            "unknown durable record collections: " + ", ".join(sorted(unknown))
        )
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
                decoded_key = loads(record.key)
                if name in {"simulation_positions", "scenario_states", "committed_ticks"}:
                    if not isinstance(decoded_key, str) or not decoded_key.strip():
                        raise ValueError(f"unowned durable temporal record in {name}")
                restored[decoded_key] = loads(record.payload)
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
            change = _dict_dirty_change(
                collection=collection,
                raw_key=raw_key,
                before_value=before_value,
                after_value=after_value,
            )
            if change is not None:
                changes.append(change)
            continue

        if isinstance(after_value, list):
            change = _list_dirty_change(
                collection=collection,
                raw_key=raw_key,
                before_value=before_value,
                after_value=after_value,
            )
            if change is not None:
                changes.append(change)
            continue

        change = _scalar_dirty_change(
            template,
            collection=collection,
            before_value=before_value,
            after_value=after_value,
        )
        if change is not None:
            changes.append(change)

    return tuple(changes)


def _semantic_record_token(value: object) -> str:
    """Compare typed SOSE values without treating map insertion order as data.

    The codec's tagged JSON distinguishes 0/0.0/False, and preserves the
    original storage bytes (including mapping insertion order). Reordering an
    otherwise identical dict is *not* a durable semantic mutation, however.
    Normalize tagged mapping/set order ONLY for the dirty comparison, never
    for the serialized payload or historical frozen evidence.
    """
    encoded = json.loads(dumps(value))

    def normalized(item):
        if isinstance(item, list):
            return [normalized(part) for part in item]
        if not isinstance(item, dict):
            return item
        result = {key: normalized(part) for key, part in item.items()}
        kind = result.get("__sose_type__")
        if kind == "dict":
            result["items"].sort(key=lambda pair: json.dumps(
                pair[0], sort_keys=True, separators=(",", ":"), ensure_ascii=False,
            ))
        elif kind in {"set", "frozenset"}:
            result["items"].sort(key=lambda member: json.dumps(
                member, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
            ))
        return result

    return json.dumps(
        normalized(encoded), sort_keys=True, separators=(",", ":"),
        ensure_ascii=False, allow_nan=False,
    )


def _dict_dirty_change(
    *,
    collection: str,
    raw_key: object,
    before_value: dict[object, object],
    after_value: dict[object, object],
) -> StateRecordChange | None:
    before_item = before_value.get(raw_key)
    exists_after = raw_key in after_value
    after_item = after_value.get(raw_key)
    # SOSE's tagged JSON is the authoritative durable representation.
    # Python equality aliases False == 0 == 0.0, including nested dataclass
    # payloads; comparing encoded records preserves semantic types.
    if (
        (raw_key in before_value) == exists_after
        and _semantic_record_token(before_item) == _semantic_record_token(after_item)
    ):
        return None
    encoded_key = dumps(raw_key)
    if not exists_after:
        return StateRecordChange(
            operation="delete",
            collection=collection,
            key=encoded_key,
        )
    position = list(after_value).index(raw_key)
    return StateRecordChange(
        operation="upsert",
        collection=collection,
        key=encoded_key,
        position=position,
        payload=dumps(after_item),
    )


def _list_dirty_change(
    *,
    collection: str,
    raw_key: object,
    before_value: list[object],
    after_value: list[object],
) -> StateRecordChange | None:
    index = int(raw_key)
    before_item = before_value[index] if index < len(before_value) else None
    after_item = after_value[index] if index < len(after_value) else None
    before_exists = index < len(before_value)
    after_exists = index < len(after_value)
    if before_exists == after_exists and (
        _semantic_record_token(before_item) == _semantic_record_token(after_item)
    ):
        return None
    if not after_exists:
        return StateRecordChange(
            operation="delete",
            collection=collection,
            key=str(index),
        )
    return StateRecordChange(
        operation="upsert",
        collection=collection,
        key=str(index),
        position=index,
        payload=dumps(after_item),
    )


def _scalar_dirty_change(
    template: _State,
    *,
    collection: str,
    before_value: object,
    after_value: object,
) -> StateRecordChange | None:
    default_value = getattr(template, collection)
    if _semantic_record_token(before_value) == _semantic_record_token(after_value):
        return None
    return StateRecordChange(
        operation="upsert",
        collection=collection,
        key="__scalar__",
        position=0,
        payload=dumps(after_value if after_value is not None else default_value),
    )

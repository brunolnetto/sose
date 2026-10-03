from __future__ import annotations

from dataclasses import fields, is_dataclass
from datetime import date, datetime
from enum import Enum
import importlib
import json
from pathlib import Path
from typing import Any
from uuid import UUID


_TAG = "__sose_type__"


def dumps(value: object) -> str:
    """Serialize trusted SOSE durable state into tagged JSON.

    The codec is intentionally explicit about Python-specific semantic types so
    SQLite persistence crosses a real serialization boundary without relying on
    pickle. Unsupported values fail instead of being coerced through repr/str.
    """

    return json.dumps(
        _encode(value),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )


def loads(payload: str) -> object:
    return _decode(json.loads(payload))


def _class_path(value: object | type[object]) -> str:
    cls = value if isinstance(value, type) else type(value)
    return f"{cls.__module__}:{cls.__qualname__}"


def _resolve(path: str) -> type[Any]:
    module_name, sep, qualname = path.partition(":")
    if not sep:
        raise ValueError(f"invalid SOSE class path: {path}")
    current: Any = importlib.import_module(module_name)
    for part in qualname.split("."):
        if part == "<locals>":
            raise ValueError(f"local classes cannot be restored: {path}")
        current = getattr(current, part)
    if not isinstance(current, type):
        raise TypeError(f"SOSE class path is not a type: {path}")
    return current


def _encode_sorted_collection(kind: str, value: set[object] | frozenset[object]) -> object:
    encoded = [_encode(item) for item in value]
    encoded.sort(key=lambda item: json.dumps(item, sort_keys=True))
    return {_TAG: kind, "items": encoded}


def _decode_collection(value: dict[str, object], factory):
    return factory(_decode(item) for item in value["items"])


def _decode_enum(value: dict[str, object]) -> object:
    cls = _resolve(str(value["class"]))
    return cls(_decode(value["value"]))


def _decode_dataclass(value: dict[str, object]) -> object:
    cls = _resolve(str(value["class"]))
    decoded_fields = {
        name: _decode(item)
        for name, item in value["fields"].items()
    }
    return cls(**decoded_fields)


def _decode_dict(value: dict[str, object]) -> object:
    return {
        _decode(key): _decode(item)
        for key, item in value["items"]
    }


_SCALAR_ENCODERS = (
    (float, lambda current: {_TAG: "float", "hex": current.hex()}),
    (datetime, lambda current: {_TAG: "datetime", "value": current.isoformat()}),
    (date, lambda current: {_TAG: "date", "value": current.isoformat()}),
    (UUID, lambda current: {_TAG: "uuid", "value": str(current)}),
    (bytes, lambda current: {_TAG: "bytes", "hex": current.hex()}),
    (Path, lambda current: {_TAG: "path", "value": str(current)}),
)


def _encode_scalar(value: object) -> object | None:
    for cls, encoder in _SCALAR_ENCODERS:
        if isinstance(value, cls):
            return encoder(value)
    return None


def _encode_collection(kind: str, value: tuple[object, ...] | list[object]) -> object:
    return {_TAG: kind, "items": [_encode(item) for item in value]}


def _encode_enum(value: object) -> object | None:
    if not isinstance(value, Enum):
        return None
    return {
        _TAG: "enum",
        "class": _class_path(value),
        "value": _encode(value.value),
    }


def _encode_dataclass_value(value: object) -> object | None:
    if not (is_dataclass(value) and not isinstance(value, type)):
        return None
    return {
        _TAG: "dataclass",
        "class": _class_path(value),
        "fields": {
            field.name: _encode(getattr(value, field.name))
            for field in fields(value)
        },
    }


def _encode_sequence_or_set(value: object) -> object | None:
    if isinstance(value, tuple):
        return _encode_collection("tuple", value)
    if isinstance(value, list):
        return _encode_collection("list", value)
    if isinstance(value, frozenset):
        return _encode_sorted_collection("frozenset", value)
    if isinstance(value, set):
        return _encode_sorted_collection("set", value)
    return None


def _encode_mapping_value(value: object) -> object | None:
    if not isinstance(value, dict):
        return None
    return {
        _TAG: "dict",
        "items": [
            [_encode(key), _encode(item)]
            for key, item in value.items()
        ],
    }


def _encode(value: object) -> object:
    if value is None or isinstance(value, (bool, int, str)):
        return value

    for encoder in (
        _encode_scalar,
        _encode_enum,
        _encode_dataclass_value,
        _encode_sequence_or_set,
        _encode_mapping_value,
    ):
        encoded = encoder(value)
        if encoded is not None:
            return encoded

    raise TypeError(
        "unsupported SOSE persistence value: "
        f"{type(value).__module__}.{type(value).__qualname__}"
    )


def _decode(value: object) -> object:
    if value is None or isinstance(value, (bool, int, str)):
        return value

    if isinstance(value, list):
        return [_decode(item) for item in value]

    if not isinstance(value, dict):
        raise TypeError(f"invalid tagged SOSE value: {type(value)!r}")

    kind = value.get(_TAG)
    if kind is None:
        return {key: _decode(item) for key, item in value.items()}

    decoders = {
        "float": lambda current: float.fromhex(str(current["hex"])),
        "datetime": lambda current: datetime.fromisoformat(str(current["value"])),
        "date": lambda current: date.fromisoformat(str(current["value"])),
        "uuid": lambda current: UUID(str(current["value"])),
        "bytes": lambda current: bytes.fromhex(str(current["hex"])),
        "path": lambda current: Path(str(current["value"])),
        "enum": _decode_enum,
        "tuple": lambda current: _decode_collection(current, tuple),
        "list": lambda current: _decode_collection(current, list),
        "frozenset": lambda current: _decode_collection(current, frozenset),
        "set": lambda current: _decode_collection(current, set),
        "dict": _decode_dict,
        "dataclass": _decode_dataclass,
    }
    decoder = decoders.get(kind)
    if decoder is not None:
        return decoder(value)

    raise ValueError(f"unknown SOSE persistence tag: {kind}")

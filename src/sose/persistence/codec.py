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


def _encode(value: object) -> object:
    if value is None or isinstance(value, (bool, int, str)):
        return value

    if isinstance(value, float):
        return {_TAG: "float", "hex": value.hex()}

    if isinstance(value, datetime):
        return {_TAG: "datetime", "value": value.isoformat()}

    if isinstance(value, date):
        return {_TAG: "date", "value": value.isoformat()}

    if isinstance(value, UUID):
        return {_TAG: "uuid", "value": str(value)}

    if isinstance(value, bytes):
        return {_TAG: "bytes", "hex": value.hex()}

    if isinstance(value, Path):
        return {_TAG: "path", "value": str(value)}

    if isinstance(value, Enum):
        return {
            _TAG: "enum",
            "class": _class_path(value),
            "value": _encode(value.value),
        }

    if is_dataclass(value) and not isinstance(value, type):
        return {
            _TAG: "dataclass",
            "class": _class_path(value),
            "fields": {
                field.name: _encode(getattr(value, field.name))
                for field in fields(value)
            },
        }

    if isinstance(value, tuple):
        return {_TAG: "tuple", "items": [_encode(item) for item in value]}

    if isinstance(value, list):
        return {_TAG: "list", "items": [_encode(item) for item in value]}

    if isinstance(value, frozenset):
        encoded = [_encode(item) for item in value]
        encoded.sort(key=lambda item: json.dumps(item, sort_keys=True))
        return {_TAG: "frozenset", "items": encoded}

    if isinstance(value, set):
        encoded = [_encode(item) for item in value]
        encoded.sort(key=lambda item: json.dumps(item, sort_keys=True))
        return {_TAG: "set", "items": encoded}

    if isinstance(value, dict):
        return {
            _TAG: "dict",
            "items": [
                [_encode(key), _encode(item)]
                for key, item in value.items()
            ],
        }

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

    if kind == "float":
        return float.fromhex(str(value["hex"]))
    if kind == "datetime":
        return datetime.fromisoformat(str(value["value"]))
    if kind == "date":
        return date.fromisoformat(str(value["value"]))
    if kind == "uuid":
        return UUID(str(value["value"]))
    if kind == "bytes":
        return bytes.fromhex(str(value["hex"]))
    if kind == "path":
        return Path(str(value["value"]))
    if kind == "enum":
        cls = _resolve(str(value["class"]))
        return cls(_decode(value["value"]))
    if kind == "tuple":
        return tuple(_decode(item) for item in value["items"])
    if kind == "list":
        return [_decode(item) for item in value["items"]]
    if kind == "frozenset":
        return frozenset(_decode(item) for item in value["items"])
    if kind == "set":
        return set(_decode(item) for item in value["items"])
    if kind == "dict":
        return {
            _decode(key): _decode(item)
            for key, item in value["items"]
        }
    if kind == "dataclass":
        cls = _resolve(str(value["class"]))
        decoded_fields = {
            name: _decode(item)
            for name, item in value["fields"].items()
        }
        return cls(**decoded_fields)

    raise ValueError(f"unknown SOSE persistence tag: {kind}")

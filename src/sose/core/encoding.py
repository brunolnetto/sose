from __future__ import annotations

from collections.abc import Mapping
from datetime import date, datetime
from enum import Enum
from uuid import UUID

_LENGTH_BYTES = 8


def encode_deterministic_parts(*parts: object) -> bytes:
    """Encode deterministic scope/identity parts without delimiter or type collisions.

    The encoding is type-tagged and length-prefixed. Unsupported object types fail
    explicitly instead of relying on potentially unstable `str(object)` output.
    """

    return b"".join(_frame(part) for part in parts)


def _frame(value: object) -> bytes:
    tag, payload = _encode(value)
    return tag + len(payload).to_bytes(_LENGTH_BYTES, "big") + payload


_SCALAR_ENCODERS = (
    (int, lambda current: (b"i", str(current).encode("ascii"))),
    (float, lambda current: (b"f", current.hex().encode("ascii"))),
    (str, lambda current: (b"s", current.encode("utf-8"))),
    (bytes, lambda current: (b"y", current)),
    (UUID, lambda current: (b"u", current.bytes)),
    (datetime, lambda current: (b"d", current.isoformat().encode("utf-8"))),
    (date, lambda current: (b"D", current.isoformat().encode("ascii"))),
)


def _encode_mapping(value: Mapping[object, object]) -> tuple[bytes, bytes]:
    encoded_items = [(_frame(key), _frame(item)) for key, item in value.items()]
    encoded_items.sort(key=lambda pair: pair[0])
    payload = len(encoded_items).to_bytes(_LENGTH_BYTES, "big")
    payload += b"".join(key + item for key, item in encoded_items)
    return b"m", payload


def _encode_scalar(value: object) -> tuple[bytes, bytes] | None:
    for cls, encoder in _SCALAR_ENCODERS:
        if isinstance(value, cls):
            return encoder(value)
    return None


def _encode(value: object) -> tuple[bytes, bytes]:
    if value is None:
        return b"n", b""

    # Enum must precede primitive checks because enums may subclass str/int.
    if isinstance(value, Enum):
        return (
            b"e",
            encode_deterministic_parts(
                type(value).__module__,
                type(value).__qualname__,
                value.value,
            ),
        )

    if isinstance(value, bool):
        return b"b", b"1" if value else b"0"
    scalar = _encode_scalar(value)
    if scalar is not None:
        return scalar

    if isinstance(value, tuple):
        return b"t", _encode_sequence(value)
    if isinstance(value, list):
        return b"l", _encode_sequence(value)

    if isinstance(value, Mapping):
        return _encode_mapping(value)

    raise TypeError(
        "deterministic scope parts must use supported stable types; "
        f"got {type(value).__module__}.{type(value).__qualname__}"
    )


def _encode_sequence(values) -> bytes:
    values = tuple(values)
    return len(values).to_bytes(_LENGTH_BYTES, "big") + b"".join(_frame(value) for value in values)

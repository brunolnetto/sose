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
    if isinstance(value, int):
        return b"i", str(value).encode("ascii")
    if isinstance(value, float):
        return b"f", value.hex().encode("ascii")
    if isinstance(value, str):
        return b"s", value.encode("utf-8")
    if isinstance(value, bytes):
        return b"y", value
    if isinstance(value, UUID):
        return b"u", value.bytes
    if isinstance(value, datetime):
        return b"d", value.isoformat().encode("utf-8")
    if isinstance(value, date):
        return b"D", value.isoformat().encode("ascii")

    if isinstance(value, tuple):
        return b"t", _encode_sequence(value)
    if isinstance(value, list):
        return b"l", _encode_sequence(value)

    if isinstance(value, Mapping):
        encoded_items = [(_frame(key), _frame(item)) for key, item in value.items()]
        encoded_items.sort(key=lambda pair: pair[0])
        payload = len(encoded_items).to_bytes(_LENGTH_BYTES, "big")
        payload += b"".join(key + item for key, item in encoded_items)
        return b"m", payload

    raise TypeError(
        "deterministic scope parts must use supported stable types; "
        f"got {type(value).__module__}.{type(value).__qualname__}"
    )


def _encode_sequence(values) -> bytes:
    values = tuple(values)
    return len(values).to_bytes(_LENGTH_BYTES, "big") + b"".join(_frame(value) for value in values)

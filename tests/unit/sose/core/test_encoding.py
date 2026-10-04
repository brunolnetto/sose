from __future__ import annotations

from datetime import date, datetime, timezone
from enum import Enum
from uuid import UUID

import pytest

from sose.core.encoding import encode_deterministic_parts


class Color(str, Enum):
    RED = "red"


def test_encoding_is_type_stable_and_order_independent_for_mappings():
    a = encode_deterministic_parts(
        None,
        True,
        42,
        3.5,
        "text",
        b"bytes",
        UUID("12345678-1234-5678-1234-567812345678"),
        datetime(2026, 1, 1, 8, tzinfo=timezone.utc),
        date(2026, 1, 2),
        Color.RED,
        ("nested", 1),
        ["list", 2],
        {"b": 2, "a": 1},
    )
    b = encode_deterministic_parts(
        None,
        True,
        42,
        3.5,
        "text",
        b"bytes",
        UUID("12345678-1234-5678-1234-567812345678"),
        datetime(2026, 1, 1, 8, tzinfo=timezone.utc),
        date(2026, 1, 2),
        Color.RED,
        ("nested", 1),
        ["list", 2],
        {"a": 1, "b": 2},
    )

    assert a == b


def test_encoding_distinguishes_values_that_stringification_would_collide():
    assert encode_deterministic_parts(1) != encode_deterministic_parts("1")
    assert encode_deterministic_parts(True) != encode_deterministic_parts(1)
    assert encode_deterministic_parts([1, 2]) != encode_deterministic_parts((1, 2))


def test_encoding_rejects_unstable_object_types():
    class Unsupported:
        pass

    with pytest.raises(TypeError, match="supported stable types"):
        encode_deterministic_parts(Unsupported())

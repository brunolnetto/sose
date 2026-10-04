from __future__ import annotations

import pytest

from tests.support.behavioral_conformance import (
    assert_conservation,
    assert_precedence,
)


@pytest.mark.parametrize(
    ("total", "buckets"),
    [
        (1, {"done": 1}),
        (5, {"done": 2, "waiting": 2, "active": 1}),
        (12, {"consumed": 7, "buffered": 3, "pending": 2}),
    ],
)
def test_conservation_accepts_partitioned_work(total, buckets):
    assert_conservation(total=total, buckets=buckets)


@pytest.mark.parametrize(
    ("completed", "edges"),
    [
        (["a", "b"], [("a", "b")]),
        (["a", "x", "b", "c"], [("a", "b"), ("b", "c")]),
    ],
)
def test_precedence_accepts_valid_partial_orders(completed, edges):
    assert_precedence(completed, edges)


def test_precedence_rejects_successor_without_predecessor():
    with pytest.raises(AssertionError):
        assert_precedence(["op-1"], [("op-0", "op-1")])


def test_conservation_rejects_lost_work():
    with pytest.raises(AssertionError):
        assert_conservation(total=4, buckets={"done": 2, "waiting": 1})

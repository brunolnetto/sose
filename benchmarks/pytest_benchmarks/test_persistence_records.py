""""Persistence record projection, reconstruction and dirty encoding."""
from __future__ import annotations

from copy import deepcopy
import os

import pytest

from sose.domain.entity import Entity
from sose.persistence.memory import _State
from sose.persistence.records import (
    changes_for_dirty_records,
    diff_state_records,
    records_to_state,
    state_to_records,
)


QUICK = os.getenv("SOSE_BENCHMARK_MODE", "full") == "quick"
SIZES = [100, 1_000] if QUICK else [100, 1_000, 10_000]
DIRTY_COUNTS = [1, 10] if QUICK else [1, 10, 100, 1_000]
ROUNDS = 1 if QUICK else 7
NOW_ATTRIBUTES = {
    "status": "ready",
    "tags": ["a", "b", "c"],
    "metadata": {"source": "benchmark"},
}


def _states(size: int, dirty_count: int):
    before = _State()
    for index in range(size):
        entity_id = f"entity-{index:06d}"
        before.entities[("benchmark", entity_id)] = Entity(
            id=entity_id,
            entity_type="benchmark",
            state="ready",
            attributes={**NOW_ATTRIBUTES, "ordinal": index, "version": 1},
        )

    after = deepcopy(before)
    dirty = []
    for index in range(dirty_count):
        key = ("benchmark", f"entity-{index:06d}")
        entity = after.entities[key]
        entity.attributes["version"] = 2
        dirty.append(("entities", key))
    return before, after, tuple(dirty)


@pytest.mark.parametrize("size", SIZES)
def test_state_to_records_scaling(benchmark, size):
    state, _, _ = _states(size, 0)
    expected = state_to_records(state)

    benchmark.pedantic(
        lambda: state_to_records(state),
        rounds=ROUNDS,
        iterations=1,
    )

    assert state_to_records(state) == expected
    benchmark.extra_info["entity_count"] = size
    benchmark.extra_info["record_count"] = len(expected)


@pytest.mark.parametrize("size", SIZES)
def test_records_to_state_scaling(benchmark, size):
    state, _, _ = _states(size, 0)
    records = state_to_records(state)

    benchmark.pedantic(
        lambda: records_to_state(records.values()),
        rounds=ROUNDS,
        iterations=1,
    )

    assert records_to_state(records.values()) == state
    benchmark.extra_info["entity_count"] = size
    benchmark.extra_info["record_count"] = len(records)


@pytest.mark.parametrize("size", SIZES)
@pytest.mark.parametrize("dirty_count", DIRTY_COUNTS)
@pytest.mark.parametrize("operation", ["full_diff", "dirty_encoding"])
def test_state_record_change_encoding_scaling(
    benchmark, size, dirty_count, operation
):
    if dirty_count > size:
        pytest.skip("dirty set cannot exceed entity count")

    before, after, dirty = _states(size, dirty_count)
    expected = diff_state_records(before, after)
    dirty_expected = changes_for_dirty_records(before, after, dirty)
    assert dirty_expected == expected

    if operation == "full_diff":
        def fn():
            return diff_state_records(before, after)
    else:
        def fn():
            return changes_for_dirty_records(before, after, dirty)

    benchmark.pedantic(fn, rounds=ROUNDS, iterations=1)

    assert fn() == expected
    benchmark.extra_info["entity_count"] = size
    benchmark.extra_info["dirty_entities"] = dirty_count
    benchmark.extra_info["dirty_fraction"] = dirty_count / size
    benchmark.extra_info["encoded_changes"] = len(expected)

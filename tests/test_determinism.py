import pytest

from sose.core.identity import deterministic_id
from sose.core.randomness import RandomSource, scoped_seed


def test_deterministic_ids_are_stable():
    assert deterministic_id("po", "supplier-1", 10) == deterministic_id("po", "supplier-1", 10)


def test_deterministic_ids_preserve_part_boundaries_and_types():
    assert deterministic_id("po", "a|b", "c") != deterministic_id("po", "a", "b|c")
    assert deterministic_id("po", 1) != deterministic_id("po", "1")


def test_scoped_rng_is_replayable():
    source = RandomSource(42)
    a = source.for_scope("tick", 10, "work_order", "wo-1").random()
    b = source.for_scope("tick", 10, "work_order", "wo-1").random()
    assert a == b


def test_scoped_seed_preserves_part_boundaries_and_types():
    assert scoped_seed(42, "a|b", "c") != scoped_seed(42, "a", "b|c")
    assert scoped_seed(42, 1) != scoped_seed(42, "1")


def test_unsupported_scope_part_fails_explicitly():
    with pytest.raises(TypeError, match="supported stable types"):
        RandomSource(42).for_scope(object())

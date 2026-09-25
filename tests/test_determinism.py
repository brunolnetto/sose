from sose.core.identity import deterministic_id
from sose.core.randomness import RandomSource


def test_deterministic_ids_are_stable():
    assert deterministic_id("po", "supplier-1", 10) == deterministic_id("po", "supplier-1", 10)


def test_scoped_rng_is_replayable():
    source = RandomSource(42)
    a = source.for_scope("tick", 10, "work_order", "wo-1").random()
    b = source.for_scope("tick", 10, "work_order", "wo-1").random()
    assert a == b

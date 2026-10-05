from __future__ import annotations

import pytest

from sose.core.randomness import CounterRandomSource, RNGMode, RandomSource, make_random_source


def test_counter_draw_is_stable_for_same_logical_key() -> None:
    rng = CounterRandomSource(root_seed=42)
    first = rng.uniform(stream="service_time", entity_id="item-1", mechanism="review", draw_index=0)
    second = rng.uniform(stream="service_time", entity_id="item-1", mechanism="review", draw_index=0)
    assert first == second
    assert 0.0 <= first < 1.0


def test_counter_draw_is_independent_of_call_order() -> None:
    rng = CounterRandomSource(root_seed=42)
    target = rng.uniform(stream="defect", entity_id="item-1", mechanism="security_review", draw_index=0)
    rng.uniform(stream="defect", entity_id="item-1", mechanism="removed_stage", draw_index=0)
    rng.uniform(stream="outage", entity_id="worker-9", mechanism="db", draw_index=0)
    assert rng.uniform(
        stream="defect", entity_id="item-1", mechanism="security_review", draw_index=0
    ) == target


def test_logical_mechanism_and_draw_index_are_part_of_identity() -> None:
    rng = CounterRandomSource(root_seed=42)
    base = rng.uniform(stream="x", entity_id="item-1", mechanism="a", draw_index=0)
    assert rng.uniform(stream="x", entity_id="item-1", mechanism="b", draw_index=0) != base
    assert rng.uniform(stream="x", entity_id="item-1", mechanism="a", draw_index=1) != base
    assert rng.uniform(stream="y", entity_id="item-1", mechanism="a", draw_index=0) != base
    assert rng.uniform(stream="x", entity_id="item-2", mechanism="a", draw_index=0) != base


def test_latent_uniform_supports_coherent_threshold_changes() -> None:
    rng = CounterRandomSource(root_seed=7)
    latent = rng.uniform(stream="defect", entity_id="item-7", mechanism="defect_propensity")
    assert rng.bernoulli(
        0.2, stream="defect", entity_id="item-7", mechanism="defect_propensity"
    ) is (latent < 0.2)
    assert rng.bernoulli(
        0.8, stream="defect", entity_id="item-7", mechanism="defect_propensity"
    ) is (latent < 0.8)


def test_bernoulli_rejects_invalid_probability() -> None:
    rng = CounterRandomSource(root_seed=7)
    with pytest.raises(ValueError, match="probability"):
        rng.bernoulli(-0.1, stream="x", entity_id="i", mechanism="m")
    with pytest.raises(ValueError, match="probability"):
        rng.bernoulli(1.1, stream="x", entity_id="i", mechanism="m")


def test_draw_index_must_be_non_negative() -> None:
    rng = CounterRandomSource(root_seed=7)
    with pytest.raises(ValueError, match="draw_index"):
        rng.uniform(stream="x", entity_id="i", mechanism="m", draw_index=-1)


def test_rng_factory_keeps_legacy_mode_and_adds_counter_mode() -> None:
    legacy = make_random_source(42, mode=RNGMode.LEGACY)
    counter = make_random_source(42, mode=RNGMode.COUNTER)
    assert isinstance(legacy, RandomSource)
    assert isinstance(counter, CounterRandomSource)
    assert legacy.for_scope("a").random() == RandomSource(42).for_scope("a").random()

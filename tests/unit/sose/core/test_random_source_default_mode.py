"""#631: make_random_source defaults to the counter RNG since 0.12.0 (announced in 0.11.0)."""
from __future__ import annotations

import warnings

import pytest

from sose.core.randomness import (
    CounterRandomSource, RNGMode, RandomSource, make_random_source,
)


def test_default_mode_is_counter_and_silent():
    with warnings.catch_warnings():
        warnings.simplefilter("error", DeprecationWarning)
        assert make_random_source(7) == CounterRandomSource(7)


@pytest.mark.parametrize("mode, expected", [
    (RNGMode.LEGACY, RandomSource(7)), ("counter", CounterRandomSource(7)),
])
def test_explicit_mode_selects_the_source(mode, expected):
    with warnings.catch_warnings():
        warnings.simplefilter("error", DeprecationWarning)
        assert make_random_source(7, mode=mode) == expected

"""#631: omitting make_random_source's mode is deprecated; the default flips in 0.12.0."""
from __future__ import annotations

import warnings

import pytest

from sose.core.randomness import (
    CounterRandomSource, RNGMode, RandomSource, make_random_source,
)


def test_implicit_mode_warns_and_keeps_the_legacy_stream():
    with pytest.warns(DeprecationWarning) as record:
        source = make_random_source(7)
    (warning,) = record
    assert warning.filename == __file__
    message = str(warning.message)
    assert "make_random_source without an explicit mode is deprecated since SOSE 0.11.0" in message
    assert "RNGMode.COUNTER" in message and "0.12.0" in message
    assert source == RandomSource(7)


@pytest.mark.parametrize("mode, expected", [
    (RNGMode.LEGACY, RandomSource(7)), ("counter", CounterRandomSource(7)),
])
def test_explicit_mode_does_not_warn(mode, expected):
    with warnings.catch_warnings():
        warnings.simplefilter("error", DeprecationWarning)
        assert make_random_source(7, mode=mode) == expected

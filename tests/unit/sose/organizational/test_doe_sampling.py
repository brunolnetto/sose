from __future__ import annotations

from math import isfinite

from sose.organizational.experiment import ParameterRange, SamplingDesign
from sose.organizational.sampling import sample_parameter_space


def _ranges() -> dict[str, ParameterRange]:
    return {
        "arrival_cv": ParameterRange(low=0.5, high=2.0),
        "service_cv": ParameterRange(low=0.25, high=3.0),
        "rework_probability": ParameterRange(low=0.0, high=0.4),
    }


def test_latin_hypercube_is_deterministic_for_seed() -> None:
    left = sample_parameter_space(
        parameter_ranges=_ranges(),
        design=SamplingDesign.LATIN_HYPERCUBE,
        sample_size=16,
        seed=20261005,
    )
    right = sample_parameter_space(
        parameter_ranges=_ranges(),
        design=SamplingDesign.LATIN_HYPERCUBE,
        sample_size=16,
        seed=20261005,
    )

    assert left == right


def test_latin_hypercube_is_stable_to_parameter_mapping_order() -> None:
    ranges = _ranges()
    reversed_ranges = dict(reversed(tuple(ranges.items())))

    assert sample_parameter_space(
        parameter_ranges=ranges,
        design=SamplingDesign.LATIN_HYPERCUBE,
        sample_size=8,
        seed=7,
    ) == sample_parameter_space(
        parameter_ranges=reversed_ranges,
        design=SamplingDesign.LATIN_HYPERCUBE,
        sample_size=8,
        seed=7,
    )


def test_latin_hypercube_places_one_point_in_each_stratum_per_dimension() -> None:
    sample_size = 12
    points = sample_parameter_space(
        parameter_ranges=_ranges(),
        design=SamplingDesign.LATIN_HYPERCUBE,
        sample_size=sample_size,
        seed=11,
    )

    for name, parameter_range in _ranges().items():
        width = parameter_range.high - parameter_range.low
        strata = {
            min(
                sample_size - 1,
                int(((point[name] - parameter_range.low) / width) * sample_size),
            )
            for point in points
        }
        assert strata == set(range(sample_size))


def test_all_sampled_values_are_inside_half_open_configured_bounds() -> None:
    for design in SamplingDesign:
        points = sample_parameter_space(
            parameter_ranges=_ranges(),
            design=design,
            sample_size=32,
            seed=99,
        )
        assert len(points) == 32
        for point in points:
            assert tuple(point) == tuple(sorted(point))
            for name, value in point.items():
                parameter_range = _ranges()[name]
                assert parameter_range.low <= value < parameter_range.high


def test_extreme_finite_ranges_do_not_overflow_when_scaled() -> None:
    parameter_range = ParameterRange(low=-1e308, high=1e308)

    for design in SamplingDesign:
        points = sample_parameter_space(
            parameter_ranges={"extreme": parameter_range},
            design=design,
            sample_size=8,
            seed=314159,
        )
        assert all(isfinite(point["extreme"]) for point in points)
        assert all(
            parameter_range.low <= point["extreme"] < parameter_range.high
            for point in points
        )


def test_sobol_is_deterministic_and_seed_scramble_changes_sequence() -> None:
    baseline = sample_parameter_space(
        parameter_ranges=_ranges(),
        design=SamplingDesign.SOBOL,
        sample_size=16,
        seed=1,
    )
    same = sample_parameter_space(
        parameter_ranges=_ranges(),
        design=SamplingDesign.SOBOL,
        sample_size=16,
        seed=1,
    )
    changed = sample_parameter_space(
        parameter_ranges=_ranges(),
        design=SamplingDesign.SOBOL,
        sample_size=16,
        seed=2,
    )

    assert baseline == same
    assert baseline != changed
    assert len({tuple(point.items()) for point in baseline}) == len(baseline)


def test_sobol_support_limit_is_explicit() -> None:
    too_many = {
        f"p{index}": ParameterRange(low=0.0, high=1.0)
        for index in range(11)
    }

    try:
        sample_parameter_space(
            parameter_ranges=too_many,
            design=SamplingDesign.SOBOL,
            sample_size=4,
            seed=1,
        )
    except ValueError as exc:
        assert "at most 10 dimensions" in str(exc)
    else:  # pragma: no cover - assertion helper
        raise AssertionError("expected Sobol dimension guard")


def test_sobol_rejects_sequence_capacity_before_allocating_points() -> None:
    try:
        sample_parameter_space(
            parameter_ranges={"p": ParameterRange(low=0.0, high=1.0)},
            design=SamplingDesign.SOBOL,
            sample_size=(1 << 32) + 1,
            seed=1,
        )
    except ValueError as exc:
        assert "32-bit sequence" in str(exc)
    else:  # pragma: no cover - assertion helper
        raise AssertionError("expected Sobol sequence-capacity guard")


def test_sampler_rejects_empty_space_and_too_small_sample() -> None:
    for kwargs, expected in (
        ({"parameter_ranges": {}}, "parameter range"),
        ({"sample_size": 1}, "sample_size"),
    ):
        arguments = {
            "parameter_ranges": _ranges(),
            "design": SamplingDesign.LATIN_HYPERCUBE,
            "sample_size": 4,
            "seed": 1,
        }
        arguments.update(kwargs)
        try:
            sample_parameter_space(**arguments)
        except ValueError as exc:
            assert expected in str(exc)
        else:  # pragma: no cover - assertion helper
            raise AssertionError("expected validation error")

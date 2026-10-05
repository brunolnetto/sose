from __future__ import annotations

from collections.abc import Mapping

from sose.core.randomness import CounterRandomSource

from .experiment import ParameterRange, SamplingDesign


# Bratley/Fox-style primitive-polynomial parameters for dimensions 2..10.
# Each entry is (degree s, polynomial coefficient a, initial odd direction numbers m).
_SOBOL_PARAMETERS: tuple[tuple[int, int, tuple[int, ...]], ...] = (
    (1, 0, (1,)),
    (2, 1, (1, 3)),
    (3, 1, (1, 3, 1)),
    (3, 2, (1, 1, 1)),
    (4, 1, (1, 1, 3, 3)),
    (4, 4, (1, 3, 5, 13)),
    (5, 2, (1, 1, 5, 5, 17)),
    (5, 4, (1, 1, 5, 5, 5)),
    (5, 7, (1, 1, 7, 11, 19)),
)
_SOBOL_BITS = 32
_SOBOL_SCALE = float(1 << _SOBOL_BITS)


def sample_parameter_space(
    *,
    parameter_ranges: Mapping[str, ParameterRange],
    design: SamplingDesign | str,
    sample_size: int,
    seed: int,
) -> tuple[dict[str, float], ...]:
    """Generate deterministic, parameter-order-stable space-filling design points."""
    if not parameter_ranges:
        raise ValueError("at least one parameter range is required")
    if sample_size < 2:
        raise ValueError("sample_size must be at least two")

    resolved = SamplingDesign(design)
    names = tuple(sorted(parameter_ranges))
    ranges = tuple(parameter_ranges[name] for name in names)

    if resolved is SamplingDesign.LATIN_HYPERCUBE:
        unit_points = _latin_hypercube(names=names, sample_size=sample_size, seed=seed)
    else:
        unit_points = _sobol(dimensions=len(names), sample_size=sample_size, seed=seed)

    return tuple(
        {
            name: parameter_range.low
            + unit_value * (parameter_range.high - parameter_range.low)
            for name, parameter_range, unit_value in zip(names, ranges, unit_point, strict=True)
        }
        for unit_point in unit_points
    )


def _latin_hypercube(
    *,
    names: tuple[str, ...],
    sample_size: int,
    seed: int,
) -> tuple[tuple[float, ...], ...]:
    rng = CounterRandomSource(seed)
    columns: list[tuple[float, ...]] = []

    for name in names:
        permutation = tuple(
            sorted(
                range(sample_size),
                key=lambda stratum: rng.uniform(
                    stream="doe_permutation",
                    entity_id=name,
                    mechanism="latin_hypercube",
                    draw_index=stratum,
                ),
            )
        )
        column = tuple(
            (
                permutation[row]
                + rng.uniform(
                    stream="doe_jitter",
                    entity_id=name,
                    mechanism="latin_hypercube",
                    draw_index=row,
                )
            )
            / sample_size
            for row in range(sample_size)
        )
        columns.append(column)

    return tuple(
        tuple(column[row] for column in columns)
        for row in range(sample_size)
    )


def _sobol(*, dimensions: int, sample_size: int, seed: int) -> tuple[tuple[float, ...], ...]:
    if dimensions > 10:
        raise ValueError("Sobol sampler supports at most 10 dimensions")

    directions = tuple(_sobol_direction_numbers(dimension) for dimension in range(dimensions))
    rng = CounterRandomSource(seed)
    shifts = tuple(
        int(
            rng.uniform(
                stream="doe_scramble",
                entity_id=dimension,
                mechanism="sobol_digital_shift",
            )
            * (1 << _SOBOL_BITS)
        )
        for dimension in range(dimensions)
    )
    state = [0] * dimensions
    points: list[tuple[float, ...]] = []

    for index in range(sample_size):
        if index:
            direction_index = _trailing_zero_count(index) + 1
            if direction_index > _SOBOL_BITS:
                raise ValueError("Sobol sample_size exceeds supported 32-bit sequence")
            for dimension in range(dimensions):
                state[dimension] ^= directions[dimension][direction_index - 1]
        points.append(
            tuple((state[dimension] ^ shifts[dimension]) / _SOBOL_SCALE for dimension in range(dimensions))
        )

    return tuple(points)


def _sobol_direction_numbers(dimension: int) -> tuple[int, ...]:
    if dimension == 0:
        return tuple(1 << (_SOBOL_BITS - index) for index in range(1, _SOBOL_BITS + 1))

    degree, coefficient, initial = _SOBOL_PARAMETERS[dimension - 1]
    directions = [0] * (_SOBOL_BITS + 1)
    for index in range(1, degree + 1):
        directions[index] = initial[index - 1] << (_SOBOL_BITS - index)

    for index in range(degree + 1, _SOBOL_BITS + 1):
        value = directions[index - degree] ^ (directions[index - degree] >> degree)
        for offset in range(1, degree):
            if (coefficient >> (degree - 1 - offset)) & 1:
                value ^= directions[index - offset]
        directions[index] = value

    return tuple(directions[1:])


def _trailing_zero_count(value: int) -> int:
    return (value & -value).bit_length() - 1

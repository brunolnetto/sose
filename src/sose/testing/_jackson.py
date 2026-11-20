"""Jackson open queueing network traffic equations (split from analytical.py).

Solves lambda = external + lambda * routing for the per-station visit rates,
with explicit input validation and an independent residual check.
"""
from __future__ import annotations


def jackson_visit_rates(
    *,
    external: tuple[float, ...],
    routing: tuple[tuple[float, ...], ...],
    tolerance: float = 1e-12,
    max_iterations: int = 100_000,
) -> tuple[float, ...]:
    _validate_jackson_inputs(external, routing, tolerance, max_iterations)
    size = len(external)
    matrix = [
        [
            (1.0 if row == column else 0.0) - routing[column][row]
            for column in range(size)
        ]
        for row in range(size)
    ]
    rates = _solve_linear_system(matrix, list(external), tolerance=tolerance)
    if any(rate < -tolerance for rate in rates):
        raise ValueError("Jackson traffic equations produced negative visit rates")
    normalized = tuple(0.0 if abs(rate) <= tolerance else rate for rate in rates)
    _verify_jackson_residual(normalized, external, routing, tolerance)
    return normalized


def _validate_jackson_inputs(
    external: tuple[float, ...],
    routing: tuple[tuple[float, ...], ...],
    tolerance: float,
    max_iterations: int,
) -> None:
    size = len(external)
    if size == 0 or len(routing) != size or any(len(row) != size for row in routing):
        raise ValueError("routing matrix must be square and match external rates")
    if tolerance <= 0:
        raise ValueError("tolerance must be positive")
    if max_iterations < 1:
        raise ValueError("max_iterations must be at least one")
    _validate_jackson_values(external, routing, tolerance)


def _validate_jackson_values(
    external: tuple[float, ...],
    routing: tuple[tuple[float, ...], ...],
    tolerance: float,
) -> None:
    if any(rate < 0 for rate in external):
        raise ValueError("external rates must be non-negative")
    if any(probability < 0 or probability > 1 for row in routing for probability in row):
        raise ValueError("routing probabilities must be between zero and one")
    if any(sum(row) > 1.0 + tolerance for row in routing):
        raise ValueError("outgoing routing probabilities cannot exceed one")


def _verify_jackson_residual(
    normalized: tuple[float, ...],
    external: tuple[float, ...],
    routing: tuple[tuple[float, ...], ...],
    tolerance: float,
) -> None:
    size = len(external)
    residual = max(
        abs(
            normalized[j]
            - external[j]
            - sum(normalized[i] * routing[i][j] for i in range(size))
        )
        for j in range(size)
    )
    scale = max(1.0, *(abs(rate) for rate in normalized), *(abs(rate) for rate in external))
    if residual > tolerance * scale:
        raise ValueError("Jackson traffic equations failed residual verification")


def _solve_linear_system(
    matrix: list[list[float]],
    vector: list[float],
    *,
    tolerance: float,
) -> list[float]:
    # The system size is the vector's length, as it always was: a matrix/vector
    # length mismatch must keep failing instead of solving a truncated system.
    size = len(vector)
    augmented = [row[:] + [vector[index]] for index, row in enumerate(matrix)]
    _forward_eliminate(augmented, size, tolerance=tolerance)
    return _back_substitute(augmented, size, tolerance=tolerance)


def _forward_eliminate(augmented: list[list[float]], size: int, *, tolerance: float) -> None:
    """Gaussian elimination with partial pivoting, in place."""
    for column in range(size):
        pivot_row = max(range(column, size), key=lambda row: abs(augmented[row][column]))
        pivot = augmented[pivot_row][column]
        row_scale = max(1.0, *(abs(value) for value in augmented[pivot_row][:-1]))
        if abs(pivot) <= tolerance * row_scale:
            raise ValueError("Jackson traffic equations are singular or ill-conditioned")
        if pivot_row != column:
            augmented[column], augmented[pivot_row] = augmented[pivot_row], augmented[column]
        _eliminate_below(augmented, column, size)


def _eliminate_below(augmented: list[list[float]], column: int, size: int) -> None:
    pivot = augmented[column][column]
    for row in range(column + 1, size):
        factor = augmented[row][column] / pivot
        if factor == 0.0:
            continue
        for index in range(column, size + 1):
            augmented[row][index] -= factor * augmented[column][index]


def _back_substitute(
    augmented: list[list[float]], size: int, *, tolerance: float,
) -> list[float]:
    result = [0.0] * size
    for row in range(size - 1, -1, -1):
        rhs = augmented[row][size] - sum(
            augmented[row][column] * result[column]
            for column in range(row + 1, size)
        )
        pivot = augmented[row][row]
        if abs(pivot) <= tolerance:
            raise ValueError("Jackson traffic equations are singular or ill-conditioned")
        result[row] = rhs / pivot
    return result

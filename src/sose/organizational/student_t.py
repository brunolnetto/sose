"""Two-sided Student-t critical values for small paired samples (stdlib only)."""
from __future__ import annotations

from functools import lru_cache
from math import exp, lgamma, log, log1p


@lru_cache(maxsize=128)
def _student_t_critical(
    *,
    confidence_level: float,
    degrees_freedom: int,
) -> float:
    """Two-sided Student-t critical value for finite paired samples."""

    if not 0.0 < confidence_level < 1.0:
        raise ValueError("confidence_level must be between zero and one")
    if degrees_freedom < 1:
        raise ValueError("degrees_freedom must be positive")

    target = 0.5 + confidence_level / 2.0
    low = 0.0
    high = 1.0
    while _student_t_cdf(high, degrees_freedom) < target:
        high *= 2.0

    for _ in range(80):
        middle = (low + high) / 2.0
        if _student_t_cdf(middle, degrees_freedom) < target:
            low = middle
        else:
            high = middle
    return (low + high) / 2.0


def _student_t_cdf(value: float, degrees_freedom: int) -> float:
    if value == 0.0:
        return 0.5
    if value < 0.0:
        return 1.0 - _student_t_cdf(-value, degrees_freedom)

    df = float(degrees_freedom)
    x = df / (df + value * value)
    tail = 0.5 * _regularized_incomplete_beta(x, df / 2.0, 0.5)
    return 1.0 - tail


def _regularized_incomplete_beta(x: float, a: float, b: float) -> float:
    if x <= 0.0:
        return 0.0
    if x >= 1.0:
        return 1.0

    log_beta_term = (
        lgamma(a + b)
        - lgamma(a)
        - lgamma(b)
        + a * log(x)
        + b * log1p(-x)
    )
    beta_term = exp(log_beta_term)
    threshold = (a + 1.0) / (a + b + 2.0)
    if x < threshold:
        return beta_term * _beta_continued_fraction(a, b, x) / a
    return 1.0 - beta_term * _beta_continued_fraction(b, a, 1.0 - x) / b


def _beta_continued_fraction(a: float, b: float, x: float) -> float:
    max_iterations = 200
    epsilon = 3.0e-14
    floor = 1.0e-300

    qab = a + b
    qap = a + 1.0
    qam = a - 1.0
    c = 1.0
    d = 1.0 - qab * x / qap
    if abs(d) < floor:
        d = floor
    d = 1.0 / d
    h = d

    for iteration in range(1, max_iterations + 1):
        doubled = 2 * iteration
        numerator = (
            iteration
            * (b - iteration)
            * x
            / ((qam + doubled) * (a + doubled))
        )
        d = 1.0 + numerator * d
        if abs(d) < floor:
            d = floor
        c = 1.0 + numerator / c
        if abs(c) < floor:
            c = floor
        d = 1.0 / d
        h *= d * c

        numerator = -(
            (a + iteration)
            * (qab + iteration)
            * x
            / ((a + doubled) * (qap + doubled))
        )
        d = 1.0 + numerator * d
        if abs(d) < floor:
            d = floor
        c = 1.0 + numerator / c
        if abs(c) < floor:
            c = floor
        d = 1.0 / d
        delta = d * c
        h *= delta
        if abs(delta - 1.0) < epsilon:
            return h

    raise ValueError("Student-t beta continued fraction did not converge")

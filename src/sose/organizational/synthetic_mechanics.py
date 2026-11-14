"""Sampling and measurement mechanics shared by the A0 and A1 reference runners.

Float operations and random draws keep the exact order of the original inline
implementations; both runners stay bit-for-bit reproducible.
"""
from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from math import ceil, cos, exp, log, pi, sqrt
from statistics import fmean, median

from sose.core.randomness import CounterRandomSource


def _standard_normal(
    rng: CounterRandomSource,
    *,
    stream: str,
    entity_id: object,
    mechanism: str,
) -> float:
    u1 = max(
        rng.uniform(
            stream=stream,
            entity_id=entity_id,
            mechanism=f"{mechanism}:u1",
        ),
        1e-15,
    )
    u2 = rng.uniform(
        stream=stream,
        entity_id=entity_id,
        mechanism=f"{mechanism}:u2",
    )
    return sqrt(-2.0 * log(u1)) * cos(2.0 * pi * u2)


def _lognormal_with_mean_cv(
    *,
    mean: float,
    cv: float,
    standard_normal: float,
) -> float:
    if cv == 0.0:
        return mean
    sigma2 = log(1.0 + cv * cv)
    sigma = sqrt(sigma2)
    mu = log(mean) - sigma2 / 2.0
    return exp(mu + sigma * standard_normal)


@dataclass(frozen=True, slots=True)
class _ServiceDraw:
    service_z: float
    primary: float
    rework_latent: float
    reworked: bool
    rework: float


def _draw_service(
    rng: CounterRandomSource,
    item_index: int,
    *,
    service_capacity: float,
    service_cv: float,
    rework_probability: float,
) -> _ServiceDraw:
    service_z = _standard_normal(
        rng,
        stream="synthetic_service",
        entity_id=item_index,
        mechanism="primary",
    )
    primary = _lognormal_with_mean_cv(
        mean=1.0 / service_capacity,
        cv=service_cv,
        standard_normal=service_z,
    )
    rework_latent = rng.uniform(
        stream="synthetic_rework",
        entity_id=item_index,
        mechanism="occurs",
    )
    reworked = rework_latent < rework_probability
    rework = 0.0
    if reworked:
        rework_z = _standard_normal(
            rng,
            stream="synthetic_service",
            entity_id=item_index,
            mechanism="rework",
        )
        rework = _lognormal_with_mean_cv(
            mean=1.0 / service_capacity,
            cv=service_cv,
            standard_normal=rework_z,
        )
    return _ServiceDraw(service_z, primary, rework_latent, reworked, rework)


def _throughput_and_wip(
    items: Sequence, start: float, end: float,
) -> tuple[float, float]:
    duration = end - start
    departures = tuple(item for item in items if start < item.completed_at <= end)
    mean_wip = sum(
        max(0.0, min(item.completed_at, end) - max(item.arrival_at, start))
        for item in items
    ) / duration
    return len(departures) / duration, mean_wip


def _lead_statistics(
    items: Sequence, start: float, end: float,
) -> tuple[float, float, float, float]:
    cohort = tuple(
        item for item in items if item.arrival_at >= start and item.completed_at <= end
    )
    leads = tuple(item.lead_time for item in cohort)
    if not leads:
        return 0.0, 0.0, 0.0, 0.0
    ordered = tuple(sorted(leads))
    return (
        fmean(ordered),
        median(ordered),
        ordered[max(1, ceil(0.90 * len(ordered))) - 1],
        sum(item.reworked for item in cohort) / len(cohort),
    )

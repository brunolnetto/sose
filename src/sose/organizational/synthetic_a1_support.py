"""Sampling, actor-ledger tracking and measurement helpers of the A1 reference runner.

Float operations and random draws keep the exact order of the original inline
implementation; the runner's results are bit-for-bit reproducible.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from math import ceil, cos, exp, log, pi, sqrt
from statistics import fmean, median

from sose.core.randomness import CounterRandomSource

from .ledger import ActorCategory, ActorLedgerInterval
from .synthetic_runner import SyntheticItemRecord


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


@dataclass(slots=True)
class _ActorTrack:
    """Gap-free actor ledger under construction; ``cursor`` is its covered end."""

    model_spec_hash: str
    cursor: float = 0.0
    intervals: list[ActorLedgerInterval] = field(default_factory=list)

    def add(
        self, start: float, end: float, category: ActorCategory, **labels: object,
    ) -> None:
        self.intervals.append(
            ActorLedgerInterval(
                start=start,
                end=end,
                category=category,
                model_spec_hash=self.model_spec_hash,
                **labels,
            )
        )
        self.cursor = end

    def adapt(self, duration: float, item_id: str) -> None:
        if duration > 0.0:
            self.add(
                self.cursor, self.cursor + duration, ActorCategory.ADAPTATION,
                work_item_id=item_id, secondary_labels=("a1_batching",),
            )

    def execute(self, execution_time: float, item_id: str, *, adapted: bool) -> float:
        completed_at = self.cursor + execution_time
        if execution_time > 0.0:
            self.add(
                self.cursor, completed_at, ActorCategory.EXECUTION,
                work_item_id=item_id,
                secondary_labels=("adapted",) if adapted else (),
            )
        self.cursor = completed_at
        return completed_at

    def idle_until(self, until: float) -> None:
        if self.cursor < until:
            self.add(self.cursor, until, ActorCategory.IDLE)


def _throughput_and_wip(
    items: list[SyntheticItemRecord], start: float, end: float,
) -> tuple[float, float]:
    duration = end - start
    departures = tuple(item for item in items if start < item.completed_at <= end)
    mean_wip = sum(
        max(0.0, min(item.completed_at, end) - max(item.arrival_at, start))
        for item in items
    ) / duration
    return len(departures) / duration, mean_wip


def _lead_statistics(
    items: list[SyntheticItemRecord], start: float, end: float,
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


def _measured_time(
    intervals: list[ActorLedgerInterval], category: ActorCategory, start: float, end: float,
) -> float:
    return sum(
        max(0.0, min(interval.end, end) - max(interval.start, start))
        for interval in intervals
        if interval.category is category
    )

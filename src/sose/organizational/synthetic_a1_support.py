"""Actor-ledger tracking and adaptation measurement of the A1 reference runner.

Float operations and random draws keep the exact order of the original inline
implementation; the runner's results are bit-for-bit reproducible.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .ledger import ActorCategory, ActorLedgerInterval
from .synthetic_mechanics import (  # noqa: F401 - shared with the A0 runner
    _draw_service,
    _lead_statistics,
    _lognormal_with_mean_cv,
    _ServiceDraw,
    _standard_normal,
    _throughput_and_wip,
)


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


def _measured_time(
    intervals: list[ActorLedgerInterval], category: ActorCategory, start: float, end: float,
) -> float:
    return sum(
        max(0.0, min(interval.end, end) - max(interval.start, start))
        for interval in intervals
        if interval.category is category
    )

from __future__ import annotations

from collections import defaultdict
from enum import StrEnum
from math import isclose, isfinite

from pydantic import BaseModel, ConfigDict, Field, model_validator


_TIMESTAMP_ABS_TOL = 1e-12
_CAPACITY_ABS_TOL = 1e-12


class ItemCategory(StrEnum):
    PROCESSING = "processing"
    REWORK = "rework"
    COORDINATION = "coordination"
    RECOVERY = "recovery"
    WAITING_CALENDAR = "waiting_calendar"
    WAITING_DECISION = "waiting_decision"
    WAITING_INFORMATION = "waiting_information"
    BLOCKED_DEPENDENCY = "blocked_dependency"
    QUEUE = "queue"
    OTHER_WAIT = "other_wait"


class ActorCategory(StrEnum):
    UNAVAILABLE = "unavailable"
    IDLE = "idle"
    EXECUTION = "execution"
    COORDINATION = "coordination"
    ADAPTATION = "adaptation"
    GOVERNANCE = "governance"


class ItemLedgerInterval(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    start: float
    end: float
    primary: ItemCategory
    cause: str | None = None
    secondary_labels: tuple[str, ...] = ()
    model_spec_hash: str | None = None

    @model_validator(mode="after")
    def validate_interval(self) -> "ItemLedgerInterval":
        if not isfinite(self.start) or not isfinite(self.end):
            raise ValueError("ledger interval endpoints must be finite")
        if self.end <= self.start:
            raise ValueError("ledger interval end must be greater than start")
        return self

    @property
    def duration(self) -> float:
        return self.end - self.start

    def contains(self, instant: float) -> bool:
        return self.start <= instant < self.end


class ItemLedger(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    work_item_id: str
    intervals: tuple[ItemLedgerInterval, ...] = ()

    @model_validator(mode="after")
    def validate_intervals(self) -> "ItemLedger":
        ordered = tuple(sorted(self.intervals, key=lambda interval: (interval.start, interval.end)))
        for previous, current in zip(ordered, ordered[1:], strict=False):
            if not _time_equal(previous.end, current.start):
                raise ValueError("item ledger contains a gap or overlap")
        object.__setattr__(self, "intervals", ordered)
        return self

    @property
    def elapsed(self) -> float:
        if not self.intervals:
            return 0.0
        return self.intervals[-1].end - self.intervals[0].start

    def duration_by_primary(self) -> dict[ItemCategory, float]:
        result: dict[ItemCategory, float] = defaultdict(float)
        for interval in self.intervals:
            result[interval.primary] += interval.duration
        return dict(result)

    def assert_complete(self, *, created_at: float, observed_at: float) -> None:
        _require_finite(created_at, "created_at")
        _require_finite(observed_at, "observed_at")
        if observed_at < created_at:
            raise ValueError("observed_at cannot precede created_at")
        if not self.intervals:
            if not _time_equal(created_at, observed_at):
                raise ValueError("item ledger does not cover the observation interval")
            return
        if not _time_equal(self.intervals[0].start, created_at) or not _time_equal(
            self.intervals[-1].end,
            observed_at,
        ):
            raise ValueError("item ledger does not cover the observation interval")
        total = sum(interval.duration for interval in self.intervals)
        if not _time_equal(total, observed_at - created_at):
            raise ValueError("item ledger violates elapsed-time conservation")


class ActorLedgerInterval(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    start: float
    end: float
    category: ActorCategory
    allocation: float = Field(default=1.0, gt=0.0, le=1.0)
    work_item_id: str | None = None
    secondary_labels: tuple[str, ...] = ()
    model_spec_hash: str | None = None

    @model_validator(mode="after")
    def validate_interval(self) -> "ActorLedgerInterval":
        if not isfinite(self.start) or not isfinite(self.end):
            raise ValueError("ledger interval endpoints must be finite")
        if not isfinite(self.allocation):
            raise ValueError("actor allocation must be finite")
        if self.end <= self.start:
            raise ValueError("ledger interval end must be greater than start")
        if self.category in {ActorCategory.UNAVAILABLE, ActorCategory.IDLE} and not isclose(
            self.allocation,
            1.0,
            rel_tol=0.0,
            abs_tol=_CAPACITY_ABS_TOL,
        ):
            raise ValueError("idle and unavailable intervals require allocation=1")
        return self

    @property
    def duration(self) -> float:
        return self.end - self.start

    @property
    def actor_time(self) -> float:
        if self.category in {ActorCategory.UNAVAILABLE, ActorCategory.IDLE}:
            return 0.0
        return self.duration * self.allocation


class ActorLedger(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    actor_id: str
    intervals: tuple[ActorLedgerInterval, ...] = ()
    fractional_multitasking: bool = False

    @model_validator(mode="after")
    def validate_capacity(self) -> "ActorLedger":
        ordered = tuple(sorted(self.intervals, key=lambda interval: (interval.start, interval.end)))
        object.__setattr__(self, "intervals", ordered)
        if self.fractional_multitasking:
            self._validate_fractional_capacity()
        else:
            for previous, current in zip(ordered, ordered[1:], strict=False):
                if current.start < previous.end:
                    raise ValueError("overlapping actor capacity is not allowed in serial mode")
        return self

    def _validate_fractional_capacity(self) -> None:
        boundaries = sorted(
            {point for interval in self.intervals for point in (interval.start, interval.end)}
        )
        for start, end in zip(boundaries, boundaries[1:], strict=False):
            if start == end:
                continue
            active = [
                interval
                for interval in self.intervals
                if interval.start < end and interval.end > start
            ]
            if not active:
                continue
            if any(
                interval.category in {ActorCategory.UNAVAILABLE, ActorCategory.IDLE}
                for interval in active
            ) and len(active) > 1:
                raise ValueError("idle or unavailable time cannot overlap actor capacity use")
            allocation = sum(
                interval.allocation
                for interval in active
                if interval.category not in {ActorCategory.UNAVAILABLE, ActorCategory.IDLE}
            )
            if allocation > 1.0 + _CAPACITY_ABS_TOL:
                raise ValueError("fractional allocation exceeds actor capacity")

    @property
    def allocated_actor_time(self) -> float:
        return sum(interval.actor_time for interval in self.intervals)

    @property
    def unavailable_time(self) -> float:
        return sum(
            interval.duration
            for interval in self.intervals
            if interval.category is ActorCategory.UNAVAILABLE
        )

    @property
    def idle_time(self) -> float:
        return sum(
            interval.duration
            for interval in self.intervals
            if interval.category is ActorCategory.IDLE
        )

    @property
    def available_time(self) -> float:
        return _union_duration(
            [
                (interval.start, interval.end)
                for interval in self.intervals
                if interval.category is not ActorCategory.UNAVAILABLE
            ]
        )

    @property
    def utilization(self) -> float:
        if _time_equal(self.available_time, 0.0):
            return 0.0
        return self.allocated_actor_time / self.available_time

    def duration_by_category(self) -> dict[ActorCategory, float]:
        result: dict[ActorCategory, float] = defaultdict(float)
        for interval in self.intervals:
            result[interval.category] += interval.duration * interval.allocation
        return dict(result)

    def assert_complete(self, *, start: float, end: float) -> None:
        _require_finite(start, "start")
        _require_finite(end, "end")
        if end < start:
            raise ValueError("end cannot precede start")
        covered = _union_duration([(interval.start, interval.end) for interval in self.intervals])
        if not self.intervals:
            if not _time_equal(start, end):
                raise ValueError("actor ledger does not cover the observation interval")
            return
        if not _time_equal(min(interval.start for interval in self.intervals), start) or not _time_equal(
            max(interval.end for interval in self.intervals),
            end,
        ):
            raise ValueError("actor ledger does not cover the observation interval")
        if not _time_equal(covered, end - start):
            raise ValueError("actor ledger violates calendar-time conservation")


def _union_duration(intervals: list[tuple[float, float]]) -> float:
    if not intervals:
        return 0.0
    ordered = sorted(intervals)
    total = 0.0
    start, end = ordered[0]
    for next_start, next_end in ordered[1:]:
        if next_start < end or _time_equal(next_start, end):
            end = max(end, next_end)
        else:
            total += end - start
            start, end = next_start, next_end
    return total + end - start


def _time_equal(left: float, right: float) -> bool:
    return isclose(left, right, rel_tol=0.0, abs_tol=_TIMESTAMP_ABS_TOL)


def _require_finite(value: float, name: str) -> None:
    if not isfinite(value):
        raise ValueError(f"{name} must be finite")

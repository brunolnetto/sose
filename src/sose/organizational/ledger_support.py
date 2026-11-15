"""Ledger categories, tolerances and interval helpers (split from ledger)."""
from __future__ import annotations

from enum import StrEnum
from math import isclose, isfinite
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .ledger import ActorLedgerInterval


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
    DEVELOPMENT = "development"
    GOVERNANCE = "governance"


# Actor time that consumes no capacity and admits no concurrent use.
_NON_CAPACITY = frozenset({ActorCategory.UNAVAILABLE, ActorCategory.IDLE})


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


def _check_config_epochs(ordered: tuple[ActorLedgerInterval, ...]) -> None:
    committed_epochs: dict[int, str] = {}
    for index, current in enumerate(ordered):
        if current.config_epoch is None:
            continue  # Older historical actor ledgers have no epoch evidence.
        previous_hash = committed_epochs.setdefault(
            current.config_epoch, current.model_spec_hash,
        )
        if previous_hash != current.model_spec_hash:
            raise ValueError("same actor config_epoch cannot change ModelSpec identity")
        for previous in ordered[:index]:
            if previous.config_epoch is None:
                continue
            if current.start < previous.end and current.config_epoch != previous.config_epoch:
                raise ValueError("actor capacity spans conflicting config epochs")
            if current.start >= previous.end and current.config_epoch < previous.config_epoch:
                raise ValueError("actor config_epoch cannot regress")


def _check_segment_capacity(active: list[ActorLedgerInterval]) -> None:
    """One elementary segment: idle/unavailable time is exclusive, use sums to <= 1."""
    if not active:
        return
    if any(
        interval.category in _NON_CAPACITY
        for interval in active
    ) and len(active) > 1:
        raise ValueError("idle or unavailable time cannot overlap actor capacity use")
    allocation = sum(
        interval.allocation
        for interval in active
        if interval.category not in _NON_CAPACITY
    )
    if allocation > 1.0 + _CAPACITY_ABS_TOL:
        raise ValueError("fractional allocation exceeds actor capacity")


# Enum class paths feed deterministic_id/scoped_seed (core/encoding.py) and the codec;
# keep them at their pre-split public home, which still re-exports them.
for _moved in (ItemCategory, ActorCategory,):
    _moved.__module__ = "sose.organizational.ledger"
del _moved

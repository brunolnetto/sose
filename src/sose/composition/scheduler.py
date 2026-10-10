"""Deterministic durable scheduling of bounded PC6 recovery triggers.

This scheduler does not use process-local counters to choose occurrences.
After every restart it derives the next slot from authoritative job checkpoints.
Use run_due from cron or a continuously running worker; the scheduled slot, not
the invocation time, defines the logical trigger identity.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Mapping

from .recovery import RecoveryTriggerResult, TradingCustomerRecoveryRunner


@dataclass(slots=True)
class RecoverySchedule:
    runner: TradingCustomerRecoveryRunner
    start_at: datetime
    interval: timedelta
    max_slots: int = 16

    def __post_init__(self) -> None:
        if self.start_at.tzinfo is None or self.start_at.utcoffset() is None:
            raise ValueError("start_at must be timezone-aware")
        if self.interval <= timedelta(0):
            raise ValueError("schedule interval must be positive")
        if self.max_slots < 1:
            raise ValueError("max_slots must be >= 1")

    def _next_slot(self) -> datetime:
        state = self.runner.persistence.job_state(self.runner.job_id)
        if state is None:
            return self.start_at
        if state.active_trigger_id is not None and state.last_triggered_at is not None:
            # An interrupted occurrence takes priority over every later slot.
            return state.last_triggered_at
        if state.completed_batch_triggers:
            last = state.completed_batch_triggers[-1].logical_time
            if last < self.start_at:
                raise ValueError("job completion precedes configured schedule anchor")
            elapsed = last - self.start_at
            if elapsed % self.interval:
                raise ValueError("job checkpoint is not aligned with schedule interval")
            return last + self.interval
        # An unfinished first occurrence may have been claimed before crash.
        if state.last_triggered_at is not None:
            return state.last_triggered_at
        return self.start_at

    def run_due(self, *, now: datetime) -> tuple[RecoveryTriggerResult, ...]:
        """Replay at most max_slots due occurrences in deterministic order."""
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("now must be timezone-aware")
        results: list[RecoveryTriggerResult] = []
        slot = self._next_slot()
        for _ in range(self.max_slots):
            if slot > now:
                break
            results.append(self.runner.run_scheduled_trigger(scheduled_for=slot, observed_at=now))
            slot = self._next_slot()
        return tuple(results)


@dataclass(frozen=True, slots=True)
class OrganizationRecoveryBatch:
    """Independent durable results; failures never hide other organizations' work."""

    completed: dict[str, tuple[RecoveryTriggerResult, ...]]
    failures: dict[str, str]


@dataclass(slots=True)
class OrganizationRecoveryFleet:
    """Dispatch existing recurring jobs without a fleet-wide clock or writer lease.

    Each organization's RecoverySchedule owns its own persistence and job
    checkpoint. A failed organization is recorded; other organizations remain
    runnable. Call a single organization's schedule directly to run workers
    concurrently in separate processes. The fleet is only a bounded dispatcher,
    never an OLTP or scheduling authority.
    """

    schedules: Mapping[str, RecoverySchedule]

    def __post_init__(self) -> None:
        if not self.schedules or any(not key for key in self.schedules):
            raise ValueError("fleet requires nonempty organization identities")
        locations: set[tuple] = set()
        for schedule in self.schedules.values():
            store = schedule.runner.persistence
            location = (
                ("postgres", store.dsn, store.namespace)
                if hasattr(store, "dsn") and hasattr(store, "namespace")
                else ("instance", id(store))
            )
            if location in locations:
                raise ValueError("independent organizations require distinct operational stores")
            locations.add(location)

    def run_due(self, *, now: datetime) -> OrganizationRecoveryBatch:
        completed: dict[str, tuple[RecoveryTriggerResult, ...]] = {}
        failures: dict[str, str] = {}
        for organization_id, schedule in sorted(self.schedules.items()):
            try:
                results = schedule.run_due(now=now)
            except Exception as exc:
                failures[organization_id] = f"{type(exc).__name__}: {exc}"
            else:
                if results:
                    completed[organization_id] = results
        return OrganizationRecoveryBatch(completed=completed, failures=failures)

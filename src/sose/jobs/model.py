from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


def _require_non_negative(value: int, *, label: str) -> None:
    if value < 0:
        raise ValueError(f"{label} must be >= 0")


@dataclass(frozen=True, slots=True)
class CompletedJobTrigger:
    trigger_id: str
    requested_ticks: int
    start_tick: int
    end_tick: int
    config_revision: int
    logical_time: datetime
    run_count: int

    def __post_init__(self) -> None:
        if not self.trigger_id:
            raise ValueError("trigger_id cannot be empty")
        if self.requested_ticks < 1:
            raise ValueError("requested_ticks must be >= 1")
        if self.start_tick < 0 or self.end_tick < self.start_tick:
            raise ValueError("invalid completed trigger tick range")


@dataclass(frozen=True, slots=True)
class SimulationJobState:
    """Durable orchestration checkpoint for one recurring simulation job."""

    job_id: str
    domain_name: str
    config_json: str
    config_revision: int
    status: str
    initialized: bool
    logical_time: datetime
    next_tick: int
    run_count: int = 0
    bootstrap_state: object | None = None
    last_triggered_at: datetime | None = None
    active_trigger_id: str | None = None
    last_completed_trigger_id: str | None = None
    active_batch_trigger_id: str | None = None
    active_batch_total_ticks: int = 0
    active_batch_completed_ticks: int = 0
    last_completed_batch_trigger_id: str | None = None
    last_completed_batch_ticks: int = 0
    completed_batch_triggers: tuple[CompletedJobTrigger, ...] = ()
    phase: str = "idle"
    last_error: str | None = None

    def __post_init__(self) -> None:
        if not self.job_id:
            raise ValueError("job_id cannot be empty")
        if not self.domain_name:
            raise ValueError("domain_name cannot be empty")
        if self.config_revision < 1:
            raise ValueError("config_revision must be >= 1")
        _require_non_negative(self.next_tick, label="next_tick")
        _require_non_negative(self.run_count, label="run_count")
        _require_non_negative(
            self.active_batch_total_ticks,
            label="active_batch_total_ticks",
        )
        _require_non_negative(
            self.active_batch_completed_ticks,
            label="active_batch_completed_ticks",
        )
        if self.active_batch_completed_ticks > self.active_batch_total_ticks:
            raise ValueError(
                "active_batch_completed_ticks cannot exceed active_batch_total_ticks"
            )
        _require_non_negative(
            self.last_completed_batch_ticks,
            label="last_completed_batch_ticks",
        )
        if self.status not in {"ready", "running", "paused", "failed"}:
            raise ValueError(f"unsupported job status: {self.status}")
        if self.phase not in {"idle", "advance", "reconcile"}:
            raise ValueError(f"unsupported job phase: {self.phase}")

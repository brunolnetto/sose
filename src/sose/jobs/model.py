from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


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
    phase: str = "idle"
    last_error: str | None = None

    def __post_init__(self) -> None:
        if not self.job_id:
            raise ValueError("job_id cannot be empty")
        if not self.domain_name:
            raise ValueError("domain_name cannot be empty")
        if self.config_revision < 1:
            raise ValueError("config_revision must be >= 1")
        if self.next_tick < 0:
            raise ValueError("next_tick must be >= 0")
        if self.run_count < 0:
            raise ValueError("run_count must be >= 0")
        if self.active_batch_total_ticks < 0:
            raise ValueError("active_batch_total_ticks must be >= 0")
        if self.active_batch_completed_ticks < 0:
            raise ValueError("active_batch_completed_ticks must be >= 0")
        if self.active_batch_completed_ticks > self.active_batch_total_ticks:
            raise ValueError(
                "active_batch_completed_ticks cannot exceed active_batch_total_ticks"
            )
        if self.last_completed_batch_ticks < 0:
            raise ValueError("last_completed_batch_ticks must be >= 0")
        if self.status not in {"ready", "running", "paused", "failed"}:
            raise ValueError(f"unsupported job status: {self.status}")
        if self.phase not in {"idle", "advance", "reconcile"}:
            raise ValueError(f"unsupported job phase: {self.phase}")

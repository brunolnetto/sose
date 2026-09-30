"""Persistent ownership/fencing integration for recurring simulation jobs."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from sose.jobs.runner import SimulationJob
from sose.persistence.ownership import FencedEnginePersistence, PersistentRunResult


@dataclass(slots=True)
class PersistentJobRunner:
    job: SimulationJob
    owner_id: str
    claim_retries: int = 3

    def __post_init__(self) -> None:
        if not self.owner_id:
            raise ValueError("owner_id cannot be empty")
        if self.claim_retries < 1:
            raise ValueError("claim_retries must be >= 1")
        for method in ("writer_epoch", "claim_writer"):
            if not callable(getattr(self.job.persistence, method, None)):
                raise TypeError(
                    "persistent authoritative runner requires ownership-capable "
                    f"EnginePersistence ({method} is missing)"
                )

    def _claim(self):
        last_error = None
        for _ in range(self.claim_retries):
            expected = self.job.persistence.writer_epoch()
            try:
                return self.job.persistence.claim_writer(
                    self.owner_id, expected_epoch=expected
                )
            except Exception as exc:
                last_error = exc
        assert last_error is not None
        raise last_error

    def _fenced_job(self, lease) -> SimulationJob:
        persistence = FencedEnginePersistence(self.job.persistence, lease)
        return SimulationJob(
            job_id=self.job.job_id,
            definition=self.job.definition,
            persistence=persistence,
            backend_factory=self.job.backend_factory,
            ticks_per_trigger=self.job.ticks_per_trigger,
            max_ticks_per_trigger=self.job.max_ticks_per_trigger,
            sink_bindings=self.job.sink_bindings,
        )

    def run_tick(self, **kwargs) -> PersistentRunResult:
        lease = self._claim()
        result = self._fenced_job(lease).run_tick(**kwargs)
        return PersistentRunResult(self.owner_id, lease.epoch, result)

    def run_trigger(self, **kwargs) -> PersistentRunResult:
        lease = self._claim()
        result = self._fenced_job(lease).run_trigger(**kwargs)
        return PersistentRunResult(self.owner_id, lease.epoch, result)

    def run_scheduled_trigger(self, **kwargs) -> PersistentRunResult:
        lease = self._claim()
        result = self._fenced_job(lease).run_scheduled_trigger(**kwargs)
        return PersistentRunResult(self.owner_id, lease.epoch, result)

"""Persistent ownership/fencing integration for recurring simulation jobs."""

from __future__ import annotations

import inspect
from dataclasses import dataclass

from sose.jobs.runner import SimulationJob
from sose.persistence.ownership import FencedEnginePersistence, PersistentRunResult


@dataclass(slots=True)
class PersistentJobRunner:
    """Claim a job-scoped writer lease, then run the job through a fenced view.

    The lease is scoped to ``job.job_id`` (#637): independent jobs in one namespace
    never fence each other out, and a takeover within one job fences the previous
    worker. Persistence must offer scoped writers (``scoped_writer_epoch``,
    ``claim_scoped_writer``) and a ``transaction`` that accepts ``writer_scope``; the
    namespace-wide-lease fallback deprecated in 0.9.0 was removed in 0.10.0.
    """

    job: SimulationJob
    owner_id: str
    claim_retries: int = 3

    def __post_init__(self) -> None:
        if not self.owner_id:
            raise ValueError("owner_id cannot be empty")
        if self.claim_retries < 1:
            raise ValueError("claim_retries must be >= 1")
        persistence = self.job.persistence
        for method in ("scoped_writer_epoch", "claim_scoped_writer"):
            if not callable(getattr(persistence, method, None)):
                raise TypeError(
                    "persistent authoritative runner requires scoped-writer "
                    f"EnginePersistence ({method} is missing)"
                )
        if not _accepts_writer_scope(persistence):
            raise TypeError(
                "persistent authoritative runner requires transaction(writer_scope=...)"
            )

    def _claim(self):
        persistence, scope = self.job.persistence, self.job.job_id
        # Preflight BEFORE claiming: a runner whose organizational attestation
        # differs from the checkpoint's (#496) is refused without advancing the
        # job's scoped epoch, so it cannot fence the correctly bound worker out.
        self.job.state()
        last_error = None
        for _ in range(self.claim_retries):
            # An epoch-read failure propagates at once; only claims retry.
            expected = persistence.scoped_writer_epoch(scope)
            try:
                return persistence.claim_scoped_writer(
                    scope, self.owner_id, expected_epoch=expected,
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
            organization_attestation=self.job.organization_attestation,
        )

    def initialize(self, config):
        """Register (or resume initializing) the job through a fenced transaction.

        The first registration commits the job's identity, including any
        organizational attestation, under this worker's job-scoped lease.
        """
        lease = self._claim()
        return self._fenced_job(lease).initialize(config)

    def run_tick(self, **kwargs) -> PersistentRunResult:
        lease = self._claim()
        result = self._fenced_job(lease).run_tick(**kwargs)
        return PersistentRunResult(self.owner_id, lease.epoch, result)

    def run_trigger(self, **kwargs) -> PersistentRunResult:
        lease = self._claim()
        result = self._fenced_job(lease).run_trigger(**kwargs)
        return PersistentRunResult(self.owner_id, lease.epoch, result)

    def run_scheduled_trigger(self, **kwargs) -> PersistentRunResult:
        if self.job.organization_attestation is not None:
            # Refused before claiming, so the job's worker is not fenced (#496).
            raise ValueError(
                "schedule an attested component job through "
                "AttestedComponentJob.run_scheduled_trigger"
            )
        lease = self._claim()
        result = self._fenced_job(lease).run_scheduled_trigger(**kwargs)
        return PersistentRunResult(self.owner_id, lease.epoch, result)


def _accepts_writer_scope(persistence) -> bool:
    try:
        parameters = inspect.signature(persistence.transaction).parameters
    except (TypeError, ValueError):
        return False
    return "writer_scope" in parameters or any(
        p.kind is inspect.Parameter.VAR_KEYWORD for p in parameters.values()
    )

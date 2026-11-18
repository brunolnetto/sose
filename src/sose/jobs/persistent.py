"""Persistent ownership/fencing integration for recurring simulation jobs."""

from __future__ import annotations

import inspect
from dataclasses import dataclass

from sose._deprecation import warn_deprecated
from sose.jobs.runner import SimulationJob
from sose.persistence.ownership import FencedEnginePersistence, PersistentRunResult


@dataclass(slots=True)
class PersistentJobRunner:
    """Claim a fenced writer lease, then run the job through a fenced view.

    The lease is scoped to ``job.job_id`` (#637) when the persistence offers
    scoped writers, so independent jobs in one namespace no longer fence each
    other out. Persistence that only implements the namespace-wide writer still
    works, with a deprecation warning (removed in 0.9).

    Upgrade note: global and scoped epochs do not fence each other. Stop every
    worker of the previous version before starting workers of this version on
    the same namespace.
    """

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
        if not self._scoped():
            # helper -> __post_init__ -> dataclass __init__ -> constructing caller
            warn_deprecated(
                "PersistentJobRunner on persistence without scoped writers",
                since="0.8.1", removed_in="0.9.0",
                instead="implement scoped_writer_epoch and claim_scoped_writer",
                stacklevel=4,
            )

    def _scoped(self) -> bool:
        """Scoped leases need the claim API AND a transaction that accepts the scope.

        A subclass overriding ``transaction`` without ``writer_scope`` keeps the
        namespace-wide lease (deprecated) instead of failing at its first write.
        """
        persistence = self.job.persistence
        if not all(
            callable(getattr(persistence, method, None))
            for method in ("scoped_writer_epoch", "claim_scoped_writer")
        ):
            return False
        try:
            parameters = inspect.signature(persistence.transaction).parameters
        except (TypeError, ValueError):
            return False
        return "writer_scope" in parameters or any(
            p.kind is inspect.Parameter.VAR_KEYWORD for p in parameters.values()
        )

    def _claim(self):
        persistence, scope = self.job.persistence, self.job.job_id
        scoped = self._scoped()
        last_error = None
        for _ in range(self.claim_retries):
            # As before: an epoch-read failure propagates at once; only claims retry.
            expected = (
                persistence.scoped_writer_epoch(scope) if scoped
                else persistence.writer_epoch()
            )
            try:
                if scoped:
                    return persistence.claim_scoped_writer(
                        scope, self.owner_id, expected_epoch=expected,
                    )
                return persistence.claim_writer(self.owner_id, expected_epoch=expected)
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

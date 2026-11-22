"""Durable batch-trigger claim/progress mixin, split from ``sose.jobs.runner``."""

from __future__ import annotations

from dataclasses import replace

from sose.domain.config import DomainDefinition
from sose.jobs.model import CompletedJobTrigger, SimulationJobState
from sose.persistence.base import Persistence


class _BatchTriggerMixin:
    """Claim, progress and finalization of batch triggers of a ``SimulationJob``."""

    job_id: str
    definition: DomainDefinition
    persistence: Persistence
    ticks_per_trigger: int
    max_ticks_per_trigger: int

    @staticmethod
    def _batch_record(
        state: SimulationJobState,
        trigger_id: str,
    ) -> CompletedJobTrigger | None:
        return next(
            (
                item
                for item in state.completed_batch_triggers
                if item.trigger_id == trigger_id
            ),
            None,
        )

    def _requested_ticks(
        self,
        *,
        state: SimulationJobState,
        trigger_id: str,
        ticks: int | None,
    ) -> int:
        if state.active_batch_trigger_id == trigger_id:
            requested = state.active_batch_total_ticks
            if ticks is not None and ticks != requested:
                raise RuntimeError(
                    "cannot recover batch trigger with a different tick count"
                )
            return requested

        requested = self.ticks_per_trigger if ticks is None else ticks
        if requested < 1:
            raise ValueError("ticks must be >= 1")
        if requested > self.max_ticks_per_trigger:
            raise ValueError(
                f"ticks exceeds max_ticks_per_trigger={self.max_ticks_per_trigger}"
            )
        return requested

    def _claim_batch_trigger(
        self,
        *,
        trigger_id: str,
        requested_ticks: int,
        ticks: int | None,
        recover: bool,
    ) -> tuple[SimulationJobState | None, int, CompletedJobTrigger | None]:
        with self.persistence.transaction() as uow:
            latest = uow.get_job_state(self.job_id)
            if latest is None:
                raise RuntimeError(
                    f"job disappeared before batch trigger claim: {self.job_id}"
                )

            completed = self._batch_record(latest, trigger_id)
            if completed is not None:
                return None, requested_ticks, completed

            active_claim = self._claim_existing_batch(
                latest=latest,
                trigger_id=trigger_id,
                ticks=ticks,
                recover=recover,
            )
            if active_claim is not None:
                return active_claim

            self._validate_idle_for_batch_trigger(latest)
            claimed = replace(
                latest,
                active_batch_trigger_id=trigger_id,
                active_batch_total_ticks=requested_ticks,
                active_batch_completed_ticks=0,
                active_batch_start_tick=latest.next_tick,
            )
            uow.save_job_state(claimed)
            return claimed, requested_ticks, None

    def _claim_existing_batch(
        self,
        *,
        latest: SimulationJobState,
        trigger_id: str,
        ticks: int | None,
        recover: bool,
    ) -> tuple[SimulationJobState, int, None] | None:
        if latest.active_batch_trigger_id is None:
            return None
        if latest.active_batch_trigger_id != trigger_id:
            raise RuntimeError(
                f"job has unresolved batch trigger: {self.job_id} "
                f"(trigger={latest.active_batch_trigger_id!r})"
            )
        requested_ticks = latest.active_batch_total_ticks
        if ticks is not None and ticks != requested_ticks:
            raise RuntimeError(
                "cannot recover batch trigger with a different tick count"
            )
        if not recover and latest.active_batch_completed_ticks < requested_ticks:
            raise RuntimeError(
                f"batch trigger requires explicit recovery: {trigger_id}"
            )
        return latest, requested_ticks, None

    def _validate_idle_for_batch_trigger(self, latest: SimulationJobState) -> None:
        if (
            latest.status == "ready"
            and latest.phase == "idle"
            and latest.active_trigger_id is None
        ):
            return
        raise RuntimeError(
            f"job is not idle for batch trigger: {self.job_id} "
            f"(status={latest.status!r}, phase={latest.phase!r}, "
            f"trigger={latest.active_trigger_id!r})"
        )

    def _save_batch_progress(self, *, trigger_id: str, completed: int) -> None:
        with self.persistence.transaction() as uow:
            latest = uow.get_job_state(self.job_id)
            if latest is None:
                raise RuntimeError(
                    f"job disappeared during batch trigger: {self.job_id}"
                )
            if latest.active_batch_trigger_id != trigger_id:
                raise RuntimeError(
                    f"batch trigger ownership was lost: {self.job_id}"
                )
            uow.save_job_state(
                replace(
                    latest,
                    active_batch_completed_ticks=completed,
                )
            )

    def _finalize_batch_trigger(
        self,
        *,
        trigger_id: str,
        requested_ticks: int,
        start_tick: int,
    ) -> SimulationJobState:
        with self.persistence.transaction() as uow:
            latest = uow.get_job_state(self.job_id)
            if latest is None:
                raise RuntimeError(
                    f"job disappeared while completing batch trigger: {self.job_id}"
                )
            if latest.active_batch_trigger_id != trigger_id:
                raise RuntimeError(
                    f"batch trigger ownership was lost: {self.job_id}"
                )
            completed_record = CompletedJobTrigger(
                trigger_id=trigger_id,
                requested_ticks=requested_ticks,
                start_tick=start_tick,
                end_tick=latest.next_tick,
                config_revision=latest.config_revision,
                logical_time=latest.logical_time,
                run_count=latest.run_count,
            )
            finished = replace(
                latest,
                active_batch_trigger_id=None,
                active_batch_total_ticks=0,
                active_batch_completed_ticks=0,
                active_batch_start_tick=None,
                last_completed_batch_trigger_id=trigger_id,
                last_completed_batch_ticks=requested_ticks,
                completed_batch_triggers=(
                    *latest.completed_batch_triggers,
                    completed_record,
                ),
            )
            uow.save_job_state(finished)
            return finished

"""Fenced single-tick claim/execute mixin, split from ``sose.jobs.runner``."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime
from typing import TYPE_CHECKING

from sose.domain.config import DomainDefinition
from sose.jobs.model import SimulationJobState
from sose.persistence.base import Persistence

if TYPE_CHECKING:
    from sose.jobs.runner import JobTickResult


class _TickExecutionMixin:
    """Claim, advance/reconcile and checkpointing of one ``SimulationJob`` tick."""

    job_id: str
    definition: DomainDefinition
    persistence: Persistence
    domain_persistence: object

    def _tick_trigger_id(
        self,
        state: SimulationJobState,
        trigger_id: str | None,
    ) -> str:
        return trigger_id or (
            f"{self.job_id}:tick:{state.next_tick}:run:{state.run_count + 1}"
        )

    def _expected_batch_child(
        self,
        batch_trigger_id: str,
        completed_ticks: int,
    ) -> str:
        return f"{batch_trigger_id}:tick:{completed_ticks + 1}"

    def _ensure_tick_matches_batch(
        self,
        state: SimulationJobState,
        effective_trigger_id: str,
    ) -> None:
        if state.active_batch_trigger_id is None:
            return
        expected_child = self._expected_batch_child(
            state.active_batch_trigger_id,
            state.active_batch_completed_ticks,
        )
        if effective_trigger_id != expected_child:
            raise RuntimeError(
                f"job has unresolved batch trigger: {self.job_id} "
                f"(trigger={state.active_batch_trigger_id!r}, "
                f"expected_child={expected_child!r})"
            )

    def _claim_tick(
        self,
        *,
        state: SimulationJobState,
        effective_trigger_id: str,
        triggered_at: datetime | None,
        recover: bool,
    ) -> SimulationJobState | JobTickResult:
        with self.persistence.transaction() as uow:
            latest = uow.get_job_state(self.job_id)
            if latest is None:
                raise RuntimeError(f"job disappeared before trigger claim: {self.job_id}")

            if latest.last_completed_trigger_id == effective_trigger_id:
                return self._result_from_state(latest)

            self._validate_batch_ownership_for_tick(
                latest=latest,
                state=state,
                effective_trigger_id=effective_trigger_id,
            )
            if latest.active_batch_trigger_id is None and state.active_batch_trigger_id is not None:
                raise RuntimeError(
                    f"batch trigger ownership changed before tick claim: "
                    f"{self.job_id}"
                )

            if latest.status == "paused":
                raise RuntimeError(f"job is paused: {self.job_id}")

            claimed = self._claim_tick_state(
                latest=latest,
                effective_trigger_id=effective_trigger_id,
                triggered_at=triggered_at,
                recover=recover,
            )
            uow.save_job_state(claimed)
            return claimed

    def _validate_batch_ownership_for_tick(
        self,
        *,
        latest: SimulationJobState,
        state: SimulationJobState,
        effective_trigger_id: str,
    ) -> None:
        if latest.active_batch_trigger_id is None:
            return
        expected_child = self._expected_batch_child(
            latest.active_batch_trigger_id,
            latest.active_batch_completed_ticks,
        )
        if effective_trigger_id != expected_child:
            raise RuntimeError(
                f"job has unresolved batch trigger: {self.job_id} "
                f"(trigger={latest.active_batch_trigger_id!r}, "
                f"expected_child={expected_child!r})"
            )

    def _claim_tick_state(
        self,
        *,
        latest: SimulationJobState,
        effective_trigger_id: str,
        triggered_at: datetime | None,
        recover: bool,
    ) -> SimulationJobState:
        unresolved = (
            latest.active_trigger_id is not None
            and latest.status in {"running", "failed"}
        )
        if not unresolved:
            return replace(
                latest,
                status="running",
                phase="advance",
                active_trigger_id=effective_trigger_id,
                last_triggered_at=triggered_at or latest.logical_time,
                last_error=None,
            )
        if not (recover and latest.active_trigger_id == effective_trigger_id):
            raise RuntimeError(
                f"job has unresolved trigger: {self.job_id} "
                f"(trigger={latest.active_trigger_id!r}, "
                f"phase={latest.phase!r})"
            )
        return replace(
            latest,
            status="running",
            last_triggered_at=triggered_at or latest.last_triggered_at,
            last_error=None,
        )

    def _save_tick_if_owned(
        self,
        *,
        effective_trigger_id: str,
        state: SimulationJobState,
    ) -> None:
        with self.persistence.transaction() as uow:
            latest = uow.get_job_state(self.job_id)
            if latest is None or latest.active_trigger_id != effective_trigger_id:
                raise RuntimeError(
                    f"job trigger ownership was lost: {self.job_id}"
                )
            uow.save_job_state(state)

    def _recover_reconcile_state(
        self,
        *,
        claimed: SimulationJobState,
        effective_trigger_id: str,
        position,
    ) -> SimulationJobState:
        if (
            claimed.phase != "advance"
            or position is None
            or position.logical_tick <= claimed.next_tick
        ):
            return claimed
        running = replace(
            claimed,
            phase="reconcile",
            logical_time=position.logical_time,
            next_tick=position.logical_tick,
        )
        self._save_tick_if_owned(
            effective_trigger_id=effective_trigger_id,
            state=running,
        )
        return running

    def _build_runtime_for_tick(self, config, logical_time: datetime, logical_tick: int):
        context, engine = self.definition.build_runtime(
            self.domain_persistence,
            config,
            logical_time,
            logical_tick,
            owner_id=self.job_id,
        )
        if engine.owner_id != self.job_id:
            raise RuntimeError("domain runtime owner does not match configured job_id")
        self._attach_domain_warehouse(engine)
        if self.transition_authority is not None:
            engine.transition_authority = self.transition_authority
        backend = self.backend_factory(logical_time)
        engine.rebuild_backend(backend)
        run_until = getattr(backend, "run_until", None)
        return context, engine, backend, run_until


    def _execute_tick_phase(
        self,
        *,
        config,
        claimed: SimulationJobState,
        effective_trigger_id: str,
    ):
        position = self.persistence.simulation_position(owner_id=self.job_id)
        running = self._recover_reconcile_state(
            claimed=claimed,
            effective_trigger_id=effective_trigger_id,
            position=position,
        )
        if running.phase != "advance":
            return self._build_reconcile_runtime(config, running=running)
        return self._advance_tick_runtime(
            config=config,
            running=running,
            effective_trigger_id=effective_trigger_id,
            position=position,
        )

    def _advance_tick_runtime(
        self,
        *,
        config,
        running: SimulationJobState,
        effective_trigger_id: str,
        position,
    ):
        logical_time = config.start_at if position is None else position.logical_time
        logical_tick = 0 if position is None else position.logical_tick
        running = replace(
            running,
            logical_time=logical_time,
            next_tick=logical_tick,
        )
        self._save_tick_if_owned(
            effective_trigger_id=effective_trigger_id,
            state=running,
        )
        context, engine, backend, run_until = self._build_runtime_for_tick(
            config,
            logical_time,
            logical_tick,
        )
        engine.advance_tick()
        if callable(run_until):
            run_until(context.clock.now)

        committed = self.persistence.simulation_position(owner_id=self.job_id)
        if committed is None:
            raise RuntimeError(
                "tick advance completed without durable simulation position"
            )
        running = replace(
            running,
            phase="reconcile",
            logical_time=committed.logical_time,
            next_tick=committed.logical_tick,
        )
        self._save_tick_if_owned(
            effective_trigger_id=effective_trigger_id,
            state=running,
        )
        return running, context, engine, backend, run_until

    def _build_reconcile_runtime(self, config, *, running: SimulationJobState):
        committed = self.persistence.simulation_position(owner_id=self.job_id)
        if committed is None:
            raise RuntimeError(
                "reconcile phase requires durable simulation position"
            )
        context, engine, backend, run_until = self._build_runtime_for_tick(
            config,
            committed.logical_time,
            committed.logical_tick,
        )
        if callable(run_until):
            run_until(context.clock.now)
        return running, context, engine, backend, run_until

    def _run_reconcile_tick(
        self,
        *,
        running: SimulationJobState,
        context,
        engine,
        backend,
        config,
        run_until,
    ) -> None:
        if self.definition.reconcile_tick is None:
            return
        self.definition.reconcile_tick(
            self.domain_persistence,
            engine,
            backend,
            config,
            running.bootstrap_state,
        )
        if callable(run_until):
            run_until(context.clock.now)

    def _persist_completed_tick(
        self,
        *,
        running: SimulationJobState,
        effective_trigger_id: str,
    ) -> SimulationJobState:
        committed = self.persistence.simulation_position(owner_id=self.job_id)
        if committed is None:
            raise RuntimeError(
                "tick completed without a durable simulation position"
            )
        completed = replace(
            running,
            status="ready",
            phase="idle",
            logical_time=committed.logical_time,
            next_tick=committed.logical_tick,
            run_count=max(running.run_count + 1, committed.logical_tick),
            active_trigger_id=None,
            last_completed_trigger_id=effective_trigger_id,
            last_error=None,
        )
        self._save_tick_if_owned(
            effective_trigger_id=effective_trigger_id,
            state=completed,
        )
        return completed

    def _persist_failed_tick(
        self,
        *,
        claimed: SimulationJobState,
        exc: Exception,
    ) -> None:
        latest = self.state() or claimed
        failed = replace(
            latest,
            status="failed",
            last_error=f"{type(exc).__name__}: {exc}",
        )
        with self.persistence.transaction() as uow:
            uow.save_job_state(failed)

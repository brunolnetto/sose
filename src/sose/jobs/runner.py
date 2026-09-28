from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime
from typing import Callable, Generic, TypeVar

from sose.domain.config import ConfigT, DomainDefinition, SeedT
from sose.jobs.model import SimulationJobState
from sose.persistence.base import Persistence


BackendFactory = Callable[[datetime], object]


@dataclass(frozen=True, slots=True)
class JobTickResult:
    job_id: str
    domain_name: str
    config_revision: int
    logical_time: datetime
    logical_tick: int
    run_count: int
    trigger_id: str | None = None


class SimulationJob(Generic[ConfigT, SeedT]):
    """Persistent one-tick-at-a-time simulation orchestration.

    Durable semantic position remains authoritative. Job state stores the
    orchestration/configuration checkpoint used by an external recurring
    trigger to resume the same simulation safely.
    """

    def __init__(
        self,
        *,
        job_id: str,
        definition: DomainDefinition[ConfigT, SeedT],
        persistence: Persistence,
        backend_factory: BackendFactory,
    ) -> None:
        if not job_id:
            raise ValueError("job_id cannot be empty")
        self.job_id = job_id
        self.definition = definition
        self.persistence = persistence
        self.backend_factory = backend_factory

    def state(self) -> SimulationJobState | None:
        return self.persistence.job_state(self.job_id)

    def initialize(
        self,
        config: ConfigT | dict[str, object] | None = None,
    ) -> SimulationJobState:
        existing = self.state()
        if existing is not None:
            if existing.domain_name != self.definition.name:
                raise ValueError(
                    f"job {self.job_id} already belongs to "
                    f"{existing.domain_name!r}"
                )
            if not existing.initialized:
                return self._finish_initialization(existing)
            return existing

        resolved = self.definition.parse_config(config)
        pending = SimulationJobState(
            job_id=self.job_id,
            domain_name=self.definition.name,
            config_json=resolved.model_dump_json(),
            config_revision=1,
            status="ready",
            initialized=False,
            logical_time=resolved.start_at,
            next_tick=0,
        )
        with self.persistence.transaction() as uow:
            uow.save_job_state(pending)
        return self._finish_initialization(pending)

    def _finish_initialization(
        self,
        pending: SimulationJobState,
    ) -> SimulationJobState:
        config = self.definition.config_model.model_validate_json(
            pending.config_json
        )
        bootstrap_state = self.definition.seed(self.persistence, config)
        initialized = replace(
            pending,
            initialized=True,
            bootstrap_state=bootstrap_state,
            status="ready",
            last_error=None,
        )
        with self.persistence.transaction() as uow:
            current = uow.get_job_state(self.job_id)
            if current is None:
                raise RuntimeError(f"job disappeared during initialization: {self.job_id}")
            if current.initialized:
                return current
            uow.save_job_state(initialized)
        return initialized

    def apply_config(
        self,
        config: ConfigT | dict[str, object],
    ) -> SimulationJobState:
        """Replace the durable domain configuration only when it changed.

        Unlike update_config(), this treats the supplied value as the complete
        desired configuration. It is the idempotent operation used by
        declarative config files and deployment tooling.
        """

        current = self.state()
        if current is None:
            return self.initialize(config)

        resolved = self.definition.parse_config(config)
        persisted = self.definition.config_model.model_validate_json(
            current.config_json
        )
        if resolved == persisted:
            return current
        return self.update_config(resolved)

    def update_config(
        self,
        config: ConfigT | dict[str, object],
    ) -> SimulationJobState:
        current = self.state()
        if current is None:
            raise RuntimeError(f"job is not initialized: {self.job_id}")
        if current.active_trigger_id is not None:
            raise RuntimeError(
                f"cannot change config while trigger is unresolved: {self.job_id}"
            )
        if isinstance(config, dict):
            previous = self.definition.config_model.model_validate_json(
                current.config_json
            )
            merged = previous.model_dump(mode="python")
            merged.update(config)
            resolved = self.definition.parse_config(merged)
        else:
            resolved = self.definition.parse_config(config)
        updated = replace(
            current,
            config_json=resolved.model_dump_json(),
            config_revision=current.config_revision + 1,
            last_error=None,
        )
        with self.persistence.transaction() as uow:
            latest = uow.get_job_state(self.job_id)
            if latest is None:
                raise RuntimeError(f"job disappeared while updating config: {self.job_id}")
            if latest.config_revision != current.config_revision:
                raise RuntimeError(
                    f"job config changed concurrently: {self.job_id}"
                )
            uow.save_job_state(updated)
        return updated

    def pause(self) -> SimulationJobState:
        return self._set_status("paused")

    def resume(self) -> SimulationJobState:
        return self._set_status("ready")

    def _set_status(self, status: str) -> SimulationJobState:
        current = self.state()
        if current is None:
            raise RuntimeError(f"job is not initialized: {self.job_id}")
        if current.status == "running":
            raise RuntimeError(
                f"cannot change status while job is running: {self.job_id}"
            )
        if (
            status == "ready"
            and current.status == "failed"
            and current.active_trigger_id is not None
        ):
            raise RuntimeError(
                f"failed trigger must be recovered before resume: {self.job_id}"
            )
        updated = replace(current, status=status)
        with self.persistence.transaction() as uow:
            uow.save_job_state(updated)
        return updated

    def _result_from_state(
        self,
        state: SimulationJobState,
    ) -> JobTickResult:
        return JobTickResult(
            job_id=self.job_id,
            domain_name=self.definition.name,
            config_revision=state.config_revision,
            logical_time=state.logical_time,
            logical_tick=state.next_tick,
            run_count=state.run_count,
            trigger_id=state.last_completed_trigger_id,
        )

    def run_tick(
        self,
        *,
        triggered_at: datetime | None = None,
        trigger_id: str | None = None,
        recover: bool = False,
    ) -> JobTickResult:
        state = self.state()
        if state is None:
            state = self.initialize()
        elif not state.initialized:
            state = self._finish_initialization(state)

        effective_trigger_id = trigger_id or (
            f"{self.job_id}:tick:{state.next_tick}:run:{state.run_count + 1}"
        )

        # Claim or explicitly recover one durable trigger. Recovery is opt-in so
        # SOSE never guesses that a currently running external worker is dead.
        with self.persistence.transaction() as uow:
            latest = uow.get_job_state(self.job_id)
            if latest is None:
                raise RuntimeError(f"job disappeared before trigger claim: {self.job_id}")

            if latest.last_completed_trigger_id == effective_trigger_id:
                return self._result_from_state(latest)

            if latest.status == "paused":
                raise RuntimeError(f"job is paused: {self.job_id}")

            unresolved = (
                latest.active_trigger_id is not None
                and latest.status in {"running", "failed"}
            )
            if unresolved:
                if not (
                    recover
                    and latest.active_trigger_id == effective_trigger_id
                ):
                    raise RuntimeError(
                        f"job has unresolved trigger: {self.job_id} "
                        f"(trigger={latest.active_trigger_id!r}, "
                        f"phase={latest.phase!r})"
                    )
                claimed = replace(
                    latest,
                    status="running",
                    last_triggered_at=triggered_at or latest.last_triggered_at,
                    last_error=None,
                )
            else:
                claimed = replace(
                    latest,
                    status="running",
                    phase="advance",
                    active_trigger_id=effective_trigger_id,
                    last_triggered_at=triggered_at or latest.logical_time,
                    last_error=None,
                )
            uow.save_job_state(claimed)

        config = self.definition.config_model.model_validate_json(
            claimed.config_json
        )

        try:
            position = self.persistence.simulation_position()

            # If semantic position advanced but the orchestration checkpoint did
            # not, a crash happened after advance_tick committed. Treat
            # SimulationPosition as authoritative and resume at reconciliation.
            if (
                claimed.phase == "advance"
                and position is not None
                and position.logical_tick > claimed.next_tick
            ):
                running = replace(
                    claimed,
                    phase="reconcile",
                    logical_time=position.logical_time,
                    next_tick=position.logical_tick,
                )
                with self.persistence.transaction() as uow:
                    latest = uow.get_job_state(self.job_id)
                    if (
                        latest is None
                        or latest.active_trigger_id != effective_trigger_id
                    ):
                        raise RuntimeError(
                            f"job trigger ownership was lost: {self.job_id}"
                        )
                    uow.save_job_state(running)
            else:
                running = claimed

            if running.phase == "advance":
                logical_time = (
                    config.start_at if position is None else position.logical_time
                )
                logical_tick = 0 if position is None else position.logical_tick
                running = replace(
                    running,
                    logical_time=logical_time,
                    next_tick=logical_tick,
                )
                with self.persistence.transaction() as uow:
                    latest = uow.get_job_state(self.job_id)
                    if (
                        latest is None
                        or latest.active_trigger_id != effective_trigger_id
                    ):
                        raise RuntimeError(
                            f"job trigger ownership was lost: {self.job_id}"
                        )
                    uow.save_job_state(running)

                context, engine = self.definition.build_runtime(
                    self.persistence,
                    config,
                    logical_time,
                    logical_tick,
                )
                backend = self.backend_factory(logical_time)
                engine.rebuild_backend(backend)

                engine.advance_tick()
                run_until = getattr(backend, "run_until", None)
                if callable(run_until):
                    run_until(context.clock.now)

                committed = self.persistence.simulation_position()
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
                with self.persistence.transaction() as uow:
                    latest = uow.get_job_state(self.job_id)
                    if (
                        latest is None
                        or latest.active_trigger_id != effective_trigger_id
                    ):
                        raise RuntimeError(
                            f"job trigger ownership was lost: {self.job_id}"
                        )
                    uow.save_job_state(running)
            else:
                committed = self.persistence.simulation_position()
                if committed is None:
                    raise RuntimeError(
                        "reconcile phase requires durable simulation position"
                    )
                context, engine = self.definition.build_runtime(
                    self.persistence,
                    config,
                    committed.logical_time,
                    committed.logical_tick,
                )
                backend = self.backend_factory(committed.logical_time)
                engine.rebuild_backend(backend)
                run_until = getattr(backend, "run_until", None)
                if callable(run_until):
                    run_until(context.clock.now)

            if self.definition.reconcile_tick is not None:
                self.definition.reconcile_tick(
                    self.persistence,
                    engine,
                    backend,
                    config,
                    running.bootstrap_state,
                )
                if callable(run_until):
                    run_until(context.clock.now)

            committed = self.persistence.simulation_position()
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
            with self.persistence.transaction() as uow:
                latest = uow.get_job_state(self.job_id)
                if (
                    latest is None
                    or latest.active_trigger_id != effective_trigger_id
                ):
                    raise RuntimeError(
                        f"job trigger ownership was lost: {self.job_id}"
                    )
                uow.save_job_state(completed)

            return self._result_from_state(completed)
        except Exception as exc:
            latest = self.state() or claimed
            failed = replace(
                latest,
                status="failed",
                last_error=f"{type(exc).__name__}: {exc}",
            )
            with self.persistence.transaction() as uow:
                uow.save_job_state(failed)
            raise

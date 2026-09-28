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

    def update_config(
        self,
        config: ConfigT | dict[str, object],
    ) -> SimulationJobState:
        current = self.state()
        if current is None:
            raise RuntimeError(f"job is not initialized: {self.job_id}")
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
        updated = replace(current, status=status)
        with self.persistence.transaction() as uow:
            uow.save_job_state(updated)
        return updated

    def run_tick(
        self,
        *,
        triggered_at: datetime | None = None,
    ) -> JobTickResult:
        state = self.state()
        if state is None:
            state = self.initialize()
        elif not state.initialized:
            state = self._finish_initialization(state)

        if state.status == "paused":
            raise RuntimeError(f"job is paused: {self.job_id}")

        config = self.definition.config_model.model_validate_json(
            state.config_json
        )

        # Durable simulation position wins over job metadata after a crash. A
        # committed tick may have advanced before the job checkpoint was saved.
        position = self.persistence.simulation_position()
        logical_time = (
            config.start_at if position is None else position.logical_time
        )
        logical_tick = 0 if position is None else position.logical_tick

        running = replace(
            state,
            status="running",
            logical_time=logical_time,
            next_tick=logical_tick,
            last_triggered_at=triggered_at or logical_time,
            last_error=None,
        )
        with self.persistence.transaction() as uow:
            uow.save_job_state(running)

        try:
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

            if self.definition.reconcile_tick is not None:
                self.definition.reconcile_tick(
                    self.persistence,
                    engine,
                    backend,
                    config,
                    state.bootstrap_state,
                )
                if callable(run_until):
                    run_until(context.clock.now)

            committed = self.persistence.simulation_position()
            if committed is None:
                raise RuntimeError("tick completed without a durable simulation position")

            completed = replace(
                running,
                status="ready",
                logical_time=committed.logical_time,
                next_tick=committed.logical_tick,
                run_count=max(state.run_count + 1, committed.logical_tick),
                last_error=None,
            )
            with self.persistence.transaction() as uow:
                uow.save_job_state(completed)

            return JobTickResult(
                job_id=self.job_id,
                domain_name=self.definition.name,
                config_revision=completed.config_revision,
                logical_time=completed.logical_time,
                logical_tick=completed.next_tick,
                run_count=completed.run_count,
            )
        except Exception as exc:
            latest = self.state() or running
            failed = replace(
                latest,
                status="failed",
                last_error=f"{type(exc).__name__}: {exc}",
            )
            with self.persistence.transaction() as uow:
                uow.save_job_state(failed)
            raise

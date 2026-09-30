from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timezone
from typing import Callable, Generic, TypeVar

from sose.domain.config import ConfigT, DomainDefinition, SeedT
from sose.domain.outbox import DomainMutationOutbox
from sose.domain.warehouse import DomainWarehouse
from sose.jobs.model import CompletedJobTrigger, SimulationJobState
from sose.persistence.base import Persistence
from sose.sinks.base import SinkBinding
from sose.sinks.outbox import SinkOutbox


BackendFactory = Callable[[datetime], object]


def scheduled_trigger_id(job_id: str, scheduled_for: datetime) -> str:
    """Return a stable trigger identity for one external scheduler occurrence."""

    if not job_id:
        raise ValueError("job_id cannot be empty")
    if scheduled_for.tzinfo is None or scheduled_for.utcoffset() is None:
        raise ValueError("scheduled_for must be timezone-aware")
    normalized = scheduled_for.astimezone(timezone.utc)
    timestamp = normalized.isoformat().replace("+00:00", "Z")
    return f"{job_id}:scheduled:{timestamp}"


@dataclass(frozen=True, slots=True)
class JobTickResult:
    job_id: str
    domain_name: str
    config_revision: int
    logical_time: datetime
    logical_tick: int
    run_count: int
    trigger_id: str | None = None


@dataclass(frozen=True, slots=True)
class JobTriggerResult:
    job_id: str
    domain_name: str
    trigger_id: str
    requested_ticks: int
    completed_ticks: int
    start_tick: int
    end_tick: int
    config_revision: int
    logical_time: datetime
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
        ticks_per_trigger: int = 1,
        max_ticks_per_trigger: int = 100,
        sink_bindings: tuple[SinkBinding, ...] = (),
        domain_warehouse: DomainWarehouse | None = None,
    ) -> None:
        if not job_id:
            raise ValueError("job_id cannot be empty")
        self.job_id = job_id
        if ticks_per_trigger < 1:
            raise ValueError("ticks_per_trigger must be >= 1")
        if max_ticks_per_trigger < 1:
            raise ValueError("max_ticks_per_trigger must be >= 1")
        if ticks_per_trigger > max_ticks_per_trigger:
            raise ValueError(
                "ticks_per_trigger cannot exceed max_ticks_per_trigger"
            )
        self.definition = definition
        self.persistence = persistence
        self.backend_factory = backend_factory
        self.ticks_per_trigger = ticks_per_trigger
        self.max_ticks_per_trigger = max_ticks_per_trigger
        self.sink_bindings = sink_bindings
        self.domain_warehouse = domain_warehouse
        self.domain_outbox = (
            DomainMutationOutbox(persistence, domain_warehouse)
            if domain_warehouse is not None
            else None
        )
        self.outbox = SinkOutbox(persistence)

    def state(self) -> SimulationJobState | None:
        return self.persistence.job_state(self.job_id)


    def flush_domain_warehouse(self) -> int:
        """Drain committed business mutations before reading the next domain state."""

        if self.domain_outbox is None:
            return 0
        return self.domain_outbox.flush()

    def _attach_domain_warehouse(self, engine) -> None:
        if self.domain_warehouse is None:
            return
        engine.domain_warehouse = self.domain_warehouse
        from sose.domain.entity_store import WarehouseBackedEntityStore
        engine.domain_entities = WarehouseBackedEntityStore(
            self.persistence, self.domain_warehouse
        )

    def pending_sink_deliveries(self):
        return tuple(
            delivery
            for delivery in self.persistence.sink_deliveries(job_id=self.job_id)
            if delivery.status == "pending"
        )

    def flush_sinks(self) -> tuple[tuple[str, str | None], ...]:
        """Best-effort drain of configured analytical sinks."""

        if not self.sink_bindings:
            return ()
        state = self.state()
        if state is None or not state.initialized:
            return ()

        results: list[tuple[str, str | None]] = []
        for binding in self.sink_bindings:
            try:
                delivery = self.outbox.flush(binding, state)
                results.append(
                    (
                        binding.name,
                        None if delivery is None else delivery.delivery_id,
                    )
                )
            except Exception as exc:
                results.append(
                    (binding.name, f"{type(exc).__name__}: {exc}")
                )
        return tuple(results)

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
        if (
            current.active_trigger_id is not None
            or current.active_batch_trigger_id is not None
        ):
            raise RuntimeError(
                f"cannot change config while trigger is unresolved: {self.job_id}"
            )
        previous = self.definition.config_model.model_validate_json(
            current.config_json
        )
        if isinstance(config, dict):
            merged = previous.model_dump(mode="python")
            merged.update(config)
            resolved = self.definition.parse_config(merged)
        else:
            resolved = self.definition.parse_config(config)

        if current.initialized:
            self.definition.validate_runtime_config_change(
                previous,
                resolved,
            )

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
        if current.status == "running" or current.active_batch_trigger_id is not None:
            raise RuntimeError(
                f"cannot change status while job trigger is active: {self.job_id}"
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

    def run_scheduled_trigger(
        self,
        *,
        scheduled_for: datetime,
        ticks: int | None = None,
        recover: bool = False,
    ) -> JobTriggerResult:
        """Run one scheduler occurrence using a deterministic timestamp identity."""

        return self.run_trigger(
            trigger_id=scheduled_trigger_id(self.job_id, scheduled_for),
            ticks=ticks,
            triggered_at=scheduled_for,
            recover=recover,
        )


    def run_trigger(
        self,
        *,
        trigger_id: str,
        ticks: int | None = None,
        triggered_at: datetime | None = None,
        recover: bool = False,
    ) -> JobTriggerResult:
        """Execute a bounded recurring trigger as one durable batch.

        The external trigger owns a stable identity. Individual ticks receive
        deterministic child trigger ids so replay after a timeout/crash can
        resume at the first unfinished tick without duplicating committed work.
        """

        if not trigger_id:
            raise ValueError("trigger_id cannot be empty")

        state = self.state()
        if state is None:
            state = self.initialize()
        elif not state.initialized:
            state = self._finish_initialization(state)

        if state.active_batch_trigger_id == trigger_id:
            requested_ticks = state.active_batch_total_ticks
            if ticks is not None and ticks != requested_ticks:
                raise RuntimeError(
                    "cannot recover batch trigger with a different tick count"
                )
        else:
            requested_ticks = self.ticks_per_trigger if ticks is None else ticks
            if requested_ticks < 1:
                raise ValueError("ticks must be >= 1")
            if requested_ticks > self.max_ticks_per_trigger:
                raise ValueError(
                    f"ticks exceeds max_ticks_per_trigger={self.max_ticks_per_trigger}"
                )

        completed_record = next(
            (
                item
                for item in state.completed_batch_triggers
                if item.trigger_id == trigger_id
            ),
            None,
        )
        if completed_record is not None:
            return JobTriggerResult(
                job_id=self.job_id,
                domain_name=self.definition.name,
                trigger_id=completed_record.trigger_id,
                requested_ticks=completed_record.requested_ticks,
                completed_ticks=completed_record.requested_ticks,
                start_tick=completed_record.start_tick,
                end_tick=completed_record.end_tick,
                config_revision=completed_record.config_revision,
                logical_time=completed_record.logical_time,
                run_count=completed_record.run_count,
            )

        with self.persistence.transaction() as uow:
            latest = uow.get_job_state(self.job_id)
            if latest is None:
                raise RuntimeError(
                    f"job disappeared before batch trigger claim: {self.job_id}"
                )

            completed_record = next(
                (
                    item
                    for item in latest.completed_batch_triggers
                    if item.trigger_id == trigger_id
                ),
                None,
            )
            if completed_record is not None:
                return JobTriggerResult(
                    job_id=self.job_id,
                    domain_name=self.definition.name,
                    trigger_id=completed_record.trigger_id,
                    requested_ticks=completed_record.requested_ticks,
                    completed_ticks=completed_record.requested_ticks,
                    start_tick=completed_record.start_tick,
                    end_tick=completed_record.end_tick,
                    config_revision=completed_record.config_revision,
                    logical_time=completed_record.logical_time,
                    run_count=completed_record.run_count,
                )

            if latest.active_batch_trigger_id is not None:
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
                claimed = latest
            else:
                if (
                    latest.status != "ready"
                    or latest.phase != "idle"
                    or latest.active_trigger_id is not None
                ):
                    raise RuntimeError(
                        f"job is not idle for batch trigger: {self.job_id} "
                        f"(status={latest.status!r}, phase={latest.phase!r}, "
                        f"trigger={latest.active_trigger_id!r})"
                    )
                claimed = replace(
                    latest,
                    active_batch_trigger_id=trigger_id,
                    active_batch_total_ticks=requested_ticks,
                    active_batch_completed_ticks=0,
                )
                uow.save_job_state(claimed)

        start_tick = claimed.next_tick - claimed.active_batch_completed_ticks
        completed = claimed.active_batch_completed_ticks

        while completed < requested_ticks:
            child_id = f"{trigger_id}:tick:{completed + 1}"
            tick_result = self.run_tick(
                triggered_at=triggered_at,
                trigger_id=child_id,
                recover=recover,
            )
            completed += 1

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
                updated = replace(
                    latest,
                    active_batch_completed_ticks=completed,
                )
                uow.save_job_state(updated)

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
                last_completed_batch_trigger_id=trigger_id,
                last_completed_batch_ticks=requested_ticks,
                completed_batch_triggers=(
                    *latest.completed_batch_triggers,
                    completed_record,
                ),
            )
            uow.save_job_state(finished)

        return JobTriggerResult(
            job_id=self.job_id,
            domain_name=self.definition.name,
            trigger_id=trigger_id,
            requested_ticks=requested_ticks,
            completed_ticks=requested_ticks,
            start_tick=start_tick,
            end_tick=finished.next_tick,
            config_revision=finished.config_revision,
            logical_time=finished.logical_time,
            run_count=finished.run_count,
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

        # Reconcile committed domain mutations before the next domain read.
        self.flush_domain_warehouse()

        # Retry any previously prepared analytical delivery before advancing.
        # Failures stay durable and never block operational semantic progress.
        self.flush_sinks()

        effective_trigger_id = trigger_id or (
            f"{self.job_id}:tick:{state.next_tick}:run:{state.run_count + 1}"
        )

        if state.active_batch_trigger_id is not None:
            expected_child_id = (
                f"{state.active_batch_trigger_id}:tick:"
                f"{state.active_batch_completed_ticks + 1}"
            )
            if effective_trigger_id != expected_child_id:
                raise RuntimeError(
                    f"job has unresolved batch trigger: {self.job_id} "
                    f"(trigger={state.active_batch_trigger_id!r}, "
                    f"expected_child={expected_child_id!r})"
                )

        # Claim or explicitly recover one durable trigger. Recovery is opt-in so
        # SOSE never guesses that a currently running external worker is dead.
        with self.persistence.transaction() as uow:
            latest = uow.get_job_state(self.job_id)
            if latest is None:
                raise RuntimeError(f"job disappeared before trigger claim: {self.job_id}")

            if latest.last_completed_trigger_id == effective_trigger_id:
                return self._result_from_state(latest)

            if latest.active_batch_trigger_id is not None:
                expected_child_id = (
                    f"{latest.active_batch_trigger_id}:tick:"
                    f"{latest.active_batch_completed_ticks + 1}"
                )
                if effective_trigger_id != expected_child_id:
                    raise RuntimeError(
                        f"job has unresolved batch trigger: {self.job_id} "
                        f"(trigger={latest.active_batch_trigger_id!r}, "
                        f"expected_child={expected_child_id!r})"
                    )
            elif state.active_batch_trigger_id is not None:
                raise RuntimeError(
                    f"batch trigger ownership changed before tick claim: "
                    f"{self.job_id}"
                )

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
                self._attach_domain_warehouse(engine)
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
                self._attach_domain_warehouse(engine)
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

            # Business mutations are committed with operational progress and are
            # delivered idempotently after that commit.
            self.flush_domain_warehouse()

            # The operational checkpoint is already durable. Analytical delivery
            # is retriable side-effect state and cannot roll this tick back.
            self.flush_sinks()
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

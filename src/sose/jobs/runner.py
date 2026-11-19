from __future__ import annotations

from dataclasses import dataclass  # noqa: F401
from datetime import datetime, timezone
from typing import Callable, Generic

from sose.domain.config import ConfigT, DomainDefinition, SeedT
from sose.domain.outbox import DomainMutationOutbox
from sose.domain.storage import DomainPersistence
from sose.domain.warehouse import DomainMutation
from sose.domain.warehouse import DomainWarehouse
from sose.jobs.model import CompletedJobTrigger, SimulationJobState
from sose.jobs.runner_batch import _BatchTriggerMixin
from sose.jobs.runner_lifecycle import _JobLifecycleMixin
from sose.jobs.runner_tick import _TickExecutionMixin
from sose.persistence.base import Persistence
from sose.sinks.base import SinkBinding
from sose.sinks.outbox import SinkOutbox


BackendFactory = Callable[[datetime], object]


def scheduled_trigger_id(job_id: str, scheduled_for: datetime) -> str:
    """Return a stable trigger identity for one external scheduler occurrence."""

    if not isinstance(job_id, str) or not job_id.strip():
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


class SimulationJob(
    _JobLifecycleMixin,
    _BatchTriggerMixin,
    _TickExecutionMixin,
    Generic[ConfigT, SeedT],
):
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
        if not isinstance(job_id, str) or not job_id.strip():
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
        self.domain_persistence = (
            DomainPersistence(persistence, domain_warehouse)
            if domain_warehouse is not None
            else persistence
        )
        self.domain_outbox = (
            DomainMutationOutbox(persistence, domain_warehouse)
            if domain_warehouse is not None
            else None
        )
        self.outbox = SinkOutbox(persistence)

    def state(self) -> SimulationJobState | None:
        return self.persistence.job_state(self.job_id)

    def close(self) -> None:
        """Close all unique stores, re-raising only after cleanup completes."""
        seen: set[int] = set()
        first_error: Exception | None = None
        for store in (self.domain_warehouse, self.persistence):
            if store is None or id(store) in seen:
                continue
            seen.add(id(store))
            close = getattr(store, "close", None)
            if callable(close):
                try:
                    close()
                except Exception as exc:
                    if first_error is None:
                        first_error = exc
        if first_error is not None:
            raise first_error



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


    def _initialized_state(self) -> SimulationJobState:
        state = self.state()
        if state is None:
            return self.initialize()
        if not state.initialized:
            return self._finish_initialization(state)
        return state

    def _batch_result(self, record: CompletedJobTrigger) -> JobTriggerResult:
        return JobTriggerResult(
            job_id=self.job_id,
            domain_name=self.definition.name,
            trigger_id=record.trigger_id,
            requested_ticks=record.requested_ticks,
            completed_ticks=record.requested_ticks,
            start_tick=record.start_tick,
            end_tick=record.end_tick,
            config_revision=record.config_revision,
            logical_time=record.logical_time,
            run_count=record.run_count,
        )

    def run_trigger(
        self,
        *,
        trigger_id: str,
        ticks: int | None = None,
        triggered_at: datetime | None = None,
        recover: bool = False,
    ) -> JobTriggerResult:
        """Execute a bounded recurring trigger as one durable batch."""
        if not trigger_id:
            raise ValueError("trigger_id cannot be empty")

        state = self._initialized_state()
        requested_ticks = self._requested_ticks(
            state=state,
            trigger_id=trigger_id,
            ticks=ticks,
        )
        completed_record = self._batch_record(state, trigger_id)
        if completed_record is not None:
            return self._batch_result(completed_record)

        claimed, requested_ticks, completed_record = self._claim_batch_trigger(
            trigger_id=trigger_id,
            requested_ticks=requested_ticks,
            ticks=ticks,
            recover=recover,
        )
        if completed_record is not None:
            return self._batch_result(completed_record)
        assert claimed is not None

        start_tick = claimed.next_tick - claimed.active_batch_completed_ticks
        completed = claimed.active_batch_completed_ticks

        while completed < requested_ticks:
            child_id = f"{trigger_id}:tick:{completed + 1}"
            self.run_tick(
                triggered_at=triggered_at,
                trigger_id=child_id,
                recover=recover,
            )
            completed += 1
            self._save_batch_progress(
                trigger_id=trigger_id,
                completed=completed,
            )

        finished = self._finalize_batch_trigger(
            trigger_id=trigger_id,
            requested_ticks=requested_ticks,
            start_tick=start_tick,
        )

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
        state = self._initialized_state()

        # Reconcile committed domain mutations before the next domain read.
        self.flush_domain_warehouse()

        # Retry any previously prepared analytical delivery before advancing.
        # Failures stay durable and never block operational semantic progress.
        self.flush_sinks()

        effective_trigger_id = self._tick_trigger_id(state, trigger_id)
        self._ensure_tick_matches_batch(state, effective_trigger_id)
        claimed_or_result = self._claim_tick(
            state=state,
            effective_trigger_id=effective_trigger_id,
            triggered_at=triggered_at,
            recover=recover,
        )
        if isinstance(claimed_or_result, JobTickResult):
            return claimed_or_result
        claimed = claimed_or_result

        config = self.definition.config_model.model_validate_json(
            claimed.config_json
        )

        try:
            running, context, engine, backend, run_until = self._execute_tick_phase(
                config=config,
                claimed=claimed,
                effective_trigger_id=effective_trigger_id,
            )
            self._run_reconcile_tick(
                running=running,
                context=context,
                engine=engine,
                backend=backend,
                config=config,
                run_until=run_until,
            )
            completed = self._persist_completed_tick(
                running=running,
                effective_trigger_id=effective_trigger_id,
            )

            # Business mutations are committed with operational progress and are
            # delivered idempotently after that commit.
            self.flush_domain_warehouse()

            # The operational checkpoint is already durable. Analytical delivery
            # is retriable side-effect state and cannot roll this tick back.
            self.flush_sinks()
            return self._result_from_state(completed)
        except Exception as exc:
            self._persist_failed_tick(claimed=claimed, exc=exc)
            raise

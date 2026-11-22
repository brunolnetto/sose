"""Job initialization, config and status mixin, split from ``sose.jobs.runner``."""

from __future__ import annotations

from dataclasses import replace

from sose.domain.config import ConfigT, DomainDefinition
from sose.jobs.model import SimulationJobState
from sose.jobs.registration import job_registration_guard
from sose.persistence.base import Persistence


class _JobLifecycleMixin:
    """Initialization, configuration and pause/resume of a ``SimulationJob``."""

    job_id: str
    definition: DomainDefinition
    persistence: Persistence
    domain_persistence: object

    def initialize(
        self,
        config: ConfigT | dict[str, object] | None = None,
    ) -> SimulationJobState:
        # A domain-specific OD-5a source and an ordinary SimulationJobState
        # cannot independently claim the same identity. Hold the SAME
        # registration fence across inspection, creation and initialization,
        # rather than an OD-5a-only lock (Postgres upsert is last-writer-wins).
        with job_registration_guard(self.persistence, self.job_id):
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
                organization_attestation=self.organization_attestation,
            )
            with self.persistence.transaction() as uow:
                # SQLite callers have no PostgreSQL advisory guard. The
                # authoritative UoW must recheck the identity under its
                # native write transaction instead of trusting the earlier
                # snapshot from state().
                concurrent = uow.get_job_state(self.job_id)
                if concurrent is not None:
                    if concurrent.domain_name != self.definition.name:
                        raise ValueError(
                            f"job {self.job_id} already belongs to "
                            f"{concurrent.domain_name!r}"
                        )
                    self._require_attestation(concurrent)
                    pending = concurrent
                else:
                    uow.save_job_state(pending)
            if pending.initialized:
                return pending
            return self._finish_initialization(pending)

    def _finish_initialization(
        self,
        pending: SimulationJobState,
    ) -> SimulationJobState:
        config = self.definition.config_model.model_validate_json(
            pending.config_json
        )
        bootstrap_state = self.definition.seed(self.domain_persistence, config, owner_id=self.job_id)
        self.flush_domain_warehouse()
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

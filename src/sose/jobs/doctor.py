from __future__ import annotations

from dataclasses import asdict, dataclass
import json
from pathlib import Path

from sose.core.diagnostics import collect_runtime_diagnostics
from sose.jobs.config import SOSEConfig, load_sose_config
from sose.jobs.model import SimulationJobState
from sose.persistence.base import Persistence
from sose.persistence.registry import (
    PersistenceRegistry,
    builtin_persistence_registry,
)


@dataclass(frozen=True, slots=True)
class JobDoctorIssue:
    code: str
    message: str


@dataclass(frozen=True, slots=True)
class JobDoctorReport:
    job_id: str
    domain_name: str
    persistence_adapter: str
    initialized: bool
    healthy: bool
    config_revision: int | None
    logical_tick: int | None
    logical_time: object | None
    status: str | None
    phase: str | None
    issues: tuple[JobDoctorIssue, ...]
    runtime_diagnostics: object

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


def inspect_job_health(
    config: SOSEConfig,
    *,
    base_dir: Path,
    persistence_registry: PersistenceRegistry | None = None,
) -> JobDoctorReport:
    """Validate declarative config and inspect durable job/runtime truth read-only."""

    from sose.examples.catalog import builtin_catalog

    domains = builtin_catalog()
    definition = domains.get(config.domain.name)
    resolved = definition.parse_config(config.domain.parameters)

    registry = persistence_registry or builtin_persistence_registry()
    registry.require(
        config.persistence.adapter,
        *config.persistence.require,
    )
    persistence = registry.create(
        config.persistence.adapter,
        config.persistence.options,
        base_dir=base_dir,
    )
    try:
        return inspect_open_job_health(
            config,
            persistence=persistence,
            resolved_config_json=resolved.model_dump_json(),
        )
    finally:
        close = getattr(persistence, "close", None)
        if callable(close):
            close()


def inspect_job_file_health(
    path: str | Path = "sose.toml",
    *,
    persistence_registry: PersistenceRegistry | None = None,
) -> JobDoctorReport:
    config, base_dir = load_sose_config(path)
    return inspect_job_health(
        config,
        base_dir=base_dir,
        persistence_registry=persistence_registry,
    )


def _append_pending_sink_delivery_issues(
    persistence: Persistence,
    *,
    state: SimulationJobState,
    issues: list[JobDoctorIssue],
) -> None:
    pending_deliveries = tuple(
        delivery
        for delivery in persistence.sink_deliveries(job_id=state.job_id)
        if delivery.status == "pending"
    )
    for delivery in pending_deliveries:
        issues.append(
            JobDoctorIssue(
                "sink.delivery_pending",
                f"sink delivery {delivery.delivery_id} to "
                f"{delivery.sink_name!r} remains pending"
                + (
                    ""
                    if delivery.last_error is None
                    else f": {delivery.last_error}"
                ),
            )
        )


def _append_position_alignment_issues(
    persistence: Persistence,
    *,
    state: SimulationJobState,
    issues: list[JobDoctorIssue],
) -> None:
    position = persistence.simulation_position()
    if position is None:
        if state.next_tick > 0:
            issues.append(
                JobDoctorIssue(
                    "job.position_missing",
                    "job checkpoint advanced but SimulationPosition is missing",
                )
            )
        return

    if position.logical_tick != state.next_tick:
        issues.append(
            JobDoctorIssue(
                "job.tick_mismatch",
                "job next_tick does not match durable SimulationPosition",
            )
        )
    if position.logical_time != state.logical_time:
        issues.append(
            JobDoctorIssue(
                "job.time_mismatch",
                "job logical_time does not match durable SimulationPosition",
            )
        )


def inspect_open_job_health(
    config: SOSEConfig,
    *,
    persistence: Persistence,
    resolved_config_json: str,
) -> JobDoctorReport:
    state = persistence.job_state(config.job.id)
    runtime = collect_runtime_diagnostics(persistence)
    issues: list[JobDoctorIssue] = [
        JobDoctorIssue(issue.code, issue.message)
        for issue in runtime.issues
    ]

    if state is not None:
        _append_pending_sink_delivery_issues(
            persistence,
            state=state,
            issues=issues,
        )
        _check_job_state(
            config,
            state,
            resolved_config_json=resolved_config_json,
            issues=issues,
        )
        _append_position_alignment_issues(
            persistence,
            state=state,
            issues=issues,
        )

    return JobDoctorReport(
        job_id=config.job.id,
        domain_name=config.domain.name,
        persistence_adapter=config.persistence.adapter,
        initialized=bool(state and state.initialized),
        healthy=not issues,
        config_revision=None if state is None else state.config_revision,
        logical_tick=None if state is None else state.next_tick,
        logical_time=None if state is None else state.logical_time,
        status=None if state is None else state.status,
        phase=None if state is None else state.phase,
        issues=tuple(issues),
        runtime_diagnostics=runtime,
    )


def _check_job_state(
    config: SOSEConfig,
    state: SimulationJobState,
    *,
    resolved_config_json: str,
    issues: list[JobDoctorIssue],
) -> None:
    if state.domain_name != config.domain.name:
        issues.append(
            JobDoctorIssue(
                "job.domain_mismatch",
                f"durable job belongs to {state.domain_name!r}, "
                f"config selects {config.domain.name!r}",
            )
        )

    try:
        durable_config = json.loads(state.config_json)
        desired_config = json.loads(resolved_config_json)
    except json.JSONDecodeError as exc:
        issues.append(
            JobDoctorIssue(
                "job.config_invalid",
                f"durable job config is not valid JSON: {exc}",
            )
        )
    else:
        if durable_config != desired_config:
            issues.append(
                JobDoctorIssue(
                    "job.config_drift",
                    "sose.toml differs from the durable config revision; "
                    "run 'sose apply' to change the job explicitly",
                )
            )

    if state.status == "failed":
        issues.append(
            JobDoctorIssue(
                "job.failed",
                state.last_error or "job is in failed state",
            )
        )

    if state.active_trigger_id is not None:
        issues.append(
            JobDoctorIssue(
                "job.trigger_unresolved",
                f"trigger {state.active_trigger_id!r} remains owned "
                f"in phase {state.phase!r}",
            )
        )

    if state.active_batch_trigger_id is not None:
        issues.append(
            JobDoctorIssue(
                "job.batch_trigger_unresolved",
                f"batch trigger {state.active_batch_trigger_id!r} remains owned "
                f"after {state.active_batch_completed_ticks}/"
                f"{state.active_batch_total_ticks} ticks",
            )
        )

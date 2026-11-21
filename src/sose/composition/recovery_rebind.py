"""Fenced re-binding of a global-lease PC6 recovery job to its job-scoped lease (#637).

Up to SOSE 0.13 a recovery job could run under the namespace-wide writer epoch
(``scoped_writer=False``, removed in 0.14.0, #635); it stores the durable identity
``{}``. A runner refuses that job until it is re-bound, because silently adopting
it could bind the wrong organization.

``rebind_recovery_job_to_scope`` is the explicit migration:

1. it claims the namespace-wide (global) writer epoch. The claim waits for the
   in-flight transactions of a running global worker and then fences it out:
   that worker's next transaction fails with ``StaleWriterError``, and a
   restarted 0.13 global worker is refused by the job's new durable identity;
2. inside one transaction fenced by that epoch it re-reads the job, checks
   that it is a trading-company recovery job in global mode with no occurrence
   in flight, checks that its durable history binds no other correlation or
   organization, reserves the correlation for the job's scope, and rewrites the
   job's config identity to ``{correlation_id, organization_id}``. ``job_id``,
   ``run_count`` and the completed-trigger history are kept unchanged.

The global epoch is then left unused. Scoped claims and scoped transactions
never consult it, so it cannot block the job's scoped workers. Running the
operation again on the already re-bound job is a no-op that claims nothing.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime
from typing import Any

from sose.composition.recovery_trigger import GLOBAL_CONFIG_JSON, scoped_config_json
from sose.jobs.model import SimulationJobState
from sose.jobs.registration import job_registration_guard

RECOVERY_DOMAIN = "trading_company_composition"


class RecoveryRebindError(ValueError):
    """The job cannot be re-bound to its scoped lease without ambiguity."""


@dataclass(frozen=True, slots=True)
class RebindReport:
    job_id: str
    correlation_id: str
    organization_id: str
    status: str  # "rebound" or "already_scoped"
    previous_config_json: str
    config_json: str
    run_count: int
    completed_triggers: int
    global_writer_epoch: int | None
    rebound_at: str


def _require_text(**values: str) -> None:
    for name, value in values.items():
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"{name} must be nonempty")


def _require_fencing(persistence: Any) -> None:
    for capability in ("writer_epoch", "claim_writer", "claim_scoped_writer",
                       "_reserve_recovery_correlation"):
        if not callable(getattr(persistence, capability, None)):
            raise TypeError("re-binding requires a backend with global and scoped writer fencing")


def _check_state(state: SimulationJobState | None, target: str) -> bool:
    """Return True when the job is already re-bound; raise unless it is rebindable."""
    if state is None:
        raise RecoveryRebindError("recovery job does not exist; nothing to re-bind")
    if state.domain_name != RECOVERY_DOMAIN:
        raise RecoveryRebindError("job is not a trading-company composition recovery job")
    if state.config_json == target:
        return True
    if state.config_json != GLOBAL_CONFIG_JSON:
        raise RecoveryRebindError(
            "recovery job is bound to another scoped identity; refusing to re-bind it"
        )
    if state.active_trigger_id is not None:
        raise RecoveryRebindError(
            "recovery occurrence is still in flight; finish it with SOSE 0.13 (a global "
            "worker run) before re-binding: 0.14 cannot resume an occurrence started "
            "under the namespace-wide lease"
        )
    return False


def _history_trigger_ids(state: SimulationJobState) -> list[str]:
    triggers = {record.trigger_id for record in state.completed_batch_triggers}
    if state.last_completed_trigger_id is not None:
        triggers.add(state.last_completed_trigger_id)
    return sorted(triggers)


def _history_bindings(persistence: Any, state: SimulationJobState) -> set[tuple[str, str]]:
    """(correlation, organization) pairs of physical effects admitted by this job's triggers.

    Only the PostgreSQL resource-intent links record which trigger admitted an
    effect. Other backends keep no such history, so nothing is determinable there.
    """
    if not hasattr(persistence, "dsn"):
        return set()
    from psycopg import sql

    links = sql.Identifier(f"{persistence.namespace}_resource_intent_link")
    connection = persistence._connection
    if connection.execute(
        "SELECT to_regclass(%s)", (links.as_string(connection),),
    ).fetchone()[0] is None:
        return set()
    rows = connection.execute(
        sql.SQL(
            "SELECT DISTINCT correlation_id, organization_id FROM {} "
            "WHERE admitted_trigger_id = ANY(%s)"
        ).format(links),
        (_history_trigger_ids(state),),
    ).fetchall()
    return {(str(correlation), str(organization)) for correlation, organization in rows}


def _check_history(
    persistence: Any, state: SimulationJobState, correlation_id: str, organization_id: str,
) -> None:
    for correlation, organization in _history_bindings(persistence, state):
        if correlation != correlation_id:
            raise RecoveryRebindError(
                f"recovery job's history includes correlation {correlation!r}; "
                "it does not own exactly one correlation"
            )
        if organization != organization_id:
            raise RecoveryRebindError(
                f"recovery job's history binds organization {organization!r}, "
                f"not {organization_id!r}"
            )


def _report(
    state: SimulationJobState, *, status: str, previous: str, epoch: int | None,
    correlation_id: str, organization_id: str, now: datetime,
) -> RebindReport:
    return RebindReport(
        job_id=state.job_id,
        correlation_id=correlation_id,
        organization_id=organization_id,
        status=status,
        previous_config_json=previous,
        config_json=state.config_json,
        run_count=state.run_count,
        completed_triggers=len(state.completed_batch_triggers),
        global_writer_epoch=epoch,
        rebound_at=now.isoformat(),
    )


def rebind_recovery_job_to_scope(
    persistence: Any,
    *,
    job_id: str,
    owner_id: str,
    correlation_id: str,
    organization_id: str,
    now: datetime,
) -> RebindReport:
    """Move a global-lease recovery job to its job-scoped lease, keeping its history."""
    _require_text(
        job_id=job_id, owner_id=owner_id,
        correlation_id=correlation_id, organization_id=organization_id,
    )
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("now must be timezone-aware")
    _require_fencing(persistence)
    target = scoped_config_json(correlation_id, organization_id)
    with job_registration_guard(persistence, job_id):
        prior = persistence.job_state(job_id)
        if _check_state(prior, target):
            return _report(
                prior, status="already_scoped", previous=prior.config_json, epoch=None,
                correlation_id=correlation_id, organization_id=organization_id, now=now,
            )
        # Refuse a determinable foreign binding BEFORE fencing a healthy worker.
        _check_history(persistence, prior, correlation_id, organization_id)
        lease = persistence.claim_writer(owner_id, expected_epoch=persistence.writer_epoch())
        with persistence.transaction(owner_epoch=lease.epoch) as uow:
            # The authoritative checks: the fenced worker may have advanced it.
            current = uow.get_job_state(job_id)
            # Already-target state is impossible under the registration guard;
            # rewriting it to the same identity would be harmless anyway.
            _check_state(current, target)
            _check_history(persistence, current, correlation_id, organization_id)
            persistence._reserve_recovery_correlation(job_id, correlation_id)
            rebound = replace(current, config_json=target)
            uow.save_job_state(rebound)
    return _report(
        rebound, status="rebound", previous=GLOBAL_CONFIG_JSON, epoch=lease.epoch,
        correlation_id=correlation_id, organization_id=organization_id, now=now,
    )

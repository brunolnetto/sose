"""Durable registration, activation and completion of one PC6 recovery trigger.

Split from recovery.py (``TradingCustomerRecoveryRunner.run_trigger``).
"""
from __future__ import annotations

from dataclasses import replace
from datetime import datetime
import json
from typing import TYPE_CHECKING, Any

from sose.jobs.model import CompletedJobTrigger, SimulationJobState
from sose.persistence.base import Persistence

if TYPE_CHECKING:
    from sose.composition.recovery import TradingCustomerRecoveryRunner


def _config_json(config: dict[str, str]) -> str:
    return json.dumps(config, sort_keys=True, separators=(',', ':'))


# The identity a namespace-wide-lease job stored up to 0.13 (#635). Such a job
# no longer runs; it is only recognised so that it can be re-bound.
GLOBAL_CONFIG_JSON = _config_json({})


def scoped_config_json(correlation_id: str, organization_id: str) -> str:
    """The durable identity of a job that runs under its job-scoped writer lease."""
    return _config_json({'correlation_id': correlation_id, 'organization_id': organization_id})


def trigger_config_json(runner: TradingCustomerRecoveryRunner) -> str:
    # Persist the operational organization even without physical
    # resource policies. A restarted job cannot silently rebind it.
    return scoped_config_json(
        runner.correlation_id, runner._organization_for(runner.correlation_id),
    )


def check_job_identity(state: SimulationJobState | None, config_json: str) -> None:
    """Reject a job identity owned by another domain or bound to another organization."""
    if state is not None and state.domain_name != "trading_company_composition":
        raise ValueError("recovery job identity already belongs to another domain")
    if state is not None and state.config_json == GLOBAL_CONFIG_JSON != config_json:
        raise ValueError(
            "recovery job was created under the namespace-wide writer lease, removed "
            "in SOSE 0.14.0; re-bind it once with sose composition-recovery --rebind-scoped "
            "(an occurrence still in flight must first be finished with SOSE 0.13)"
        )
    if state is not None and state.config_json != config_json:
        raise ValueError("recovery job's durable organizational binding changed")


def _claim_and_register(
    runner: TradingCustomerRecoveryRunner, *, config_json: str, now: datetime,
) -> Any:
    # The writer epoch and the first durable domain identity must
    # become visible in ONE OLTP commit. A real os._exit directly
    # after the claim cannot leave an ownerless fenced namespace.
    initial = SimulationJobState(
        job_id=runner.job_id,
        domain_name="trading_company_composition",
        config_json=config_json,
        config_revision=1,
        status="ready",
        initialized=True,
        logical_time=now,
        next_tick=0,
    )
    atomic_claim = getattr(runner.persistence, "claim_scoped_writer_and_register", None)
    if not callable(atomic_claim):
        raise TypeError("scoped recovery requires atomic claim+registration")
    return atomic_claim(
        runner.job_id, runner.owner_id,
        expected_epoch=runner.persistence.scoped_writer_epoch(runner.job_id),
        correlation_id=runner.correlation_id, initial_job=initial,
    )


def claim_trigger_writer(
    runner: TradingCustomerRecoveryRunner, prior: SimulationJobState | None,
    *, config_json: str, now: datetime,
) -> Any:
    """Claim the job-scoped writer lease, registering a first-seen job atomically."""
    if prior is None:
        return _claim_and_register(runner, config_json=config_json, now=now)
    return runner.persistence.claim_scoped_writer(
        runner.job_id, runner.owner_id,
        expected_epoch=runner.persistence.scoped_writer_epoch(runner.job_id),
        correlation_id=runner.correlation_id,
    )


def _already_completed(current: SimulationJobState, trigger_id: str) -> bool:
    return current.last_completed_trigger_id == trigger_id or any(
        record.trigger_id == trigger_id
        for record in current.completed_batch_triggers
    )


def activate_trigger(
    runner: TradingCustomerRecoveryRunner, uow: Any,
    *, trigger_id: str, config_json: str, now: datetime,
) -> bool:
    """Mark the occurrence running; return False when it had already completed."""
    current = uow.get_job_state(runner.job_id)
    check_job_identity(current, config_json)
    if current is None:
        raise RuntimeError("recovery writer claim lacks atomic durable job identity")
    if _already_completed(current, trigger_id):
        return False
    if now < current.logical_time:
        raise ValueError("composition trigger cannot rewind durable logical time")
    if current.active_trigger_id not in (None, trigger_id):
        raise RuntimeError("another recovery occurrence is still active")
    if current.active_trigger_id is None and (
        current.active_effect_ids or current.active_boundary_message_ids
        or current.active_publication_message_ids
    ):
        raise RuntimeError("completed recovery job has orphaned work enrollment")
    uow.save_job_state(replace(
        current,
        status="running",
        phase="reconcile",
        active_trigger_id=trigger_id,
        logical_time=now,
        last_triggered_at=now,
    ))
    return True


def complete_trigger(
    runner: TradingCustomerRecoveryRunner, store: Persistence,
    *, trigger_id: str, now: datetime,
) -> None:
    with store.transaction() as uow:
        current = uow.get_job_state(runner.job_id)
        if current is None or current.active_trigger_id != trigger_id:
            raise RuntimeError("composition recovery trigger ownership lost")
        completed_record = CompletedJobTrigger(
            trigger_id=trigger_id,
            requested_ticks=1,
            start_tick=current.next_tick,
            end_tick=current.next_tick + 1,
            config_revision=current.config_revision,
            logical_time=now,
            run_count=current.run_count + 1,
        )
        uow.save_job_state(replace(
            current,
            status="ready",
            phase="idle",
            active_trigger_id=None,
            active_effect_ids=(),
            active_boundary_message_ids=(),
            active_publication_message_ids=(),
            last_completed_trigger_id=trigger_id,
            completed_batch_triggers=(
                *current.completed_batch_triggers, completed_record,
            ),
            next_tick=current.next_tick + 1,
            run_count=current.run_count + 1,
            last_error=None,
        ))

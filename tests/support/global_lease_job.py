"""The durable footprint of a SOSE 0.13 namespace-wide-lease recovery job (#635).

The namespace-wide writer lease of ``TradingCustomerRecoveryRunner`` was removed in
0.14.0, but databases written by 0.13 still hold jobs created under it: identity
``{}``, triggers completed under the global writer epoch. ``--rebind-scoped`` exists
to migrate exactly those jobs, so its tests need them. This helper writes what a
0.13 global run with no pending work left behind, through the same trigger
registration, activation and completion code that run used (the bounded pass is
not part of the footprint: with no work it changes nothing).
"""
from __future__ import annotations

from collections.abc import Iterable, Mapping
from datetime import datetime, timedelta
from types import SimpleNamespace

from sose.composition.recovery_trigger import (
    GLOBAL_CONFIG_JSON,
    activate_trigger,
    check_job_identity,
    complete_trigger,
)
from sose.jobs.model import SimulationJobState
from sose.jobs.registration import job_registration_guard
from sose.jobs.runner import scheduled_trigger_id
from sose.persistence.ownership import FencedEnginePersistence

RECOVERY_JOB = "trading-company-customer-recovery"


def _claim_global(store, job_id: str, owner_id: str, now: datetime):
    """0.13 ``claim_trigger_writer`` for ``scoped_writer=False``."""
    prior = store.job_state(job_id)
    check_job_identity(prior, GLOBAL_CONFIG_JSON)
    if prior is not None:
        return store.claim_writer(owner_id, expected_epoch=store.writer_epoch())
    initial = SimulationJobState(
        job_id=job_id, domain_name="trading_company_composition",
        config_json=GLOBAL_CONFIG_JSON, config_revision=1, status="ready",
        initialized=True, logical_time=now, next_tick=0,
    )
    return store.claim_writer_and_register(
        owner_id, expected_epoch=store.writer_epoch(), initial_job=initial,
    )


def seed_global_lease_job(
    store, slots: Iterable[datetime], *, job_id: str = RECOVERY_JOB, owner_id: str = "worker",
    resource_policies: Mapping | None = None,
) -> int:
    """Complete one global-lease occurrence per scheduled slot; return how many ran.

    With ``resource_policies`` (PostgreSQL policy mode) the physical resource ledger is
    created as well: the 0.13 pass built its ``IntentResourceCoordinator`` every run.
    """
    if resource_policies is not None:
        from sose.composition.resource_intents import IntentResourceCoordinator

        IntentResourceCoordinator(
            store, resource_policies,
            slot_duration=timedelta(minutes=5), retry_delay=timedelta(minutes=5),
        )
    job = SimpleNamespace(job_id=job_id)
    completed = 0
    for slot in slots:
        trigger_id = scheduled_trigger_id(job_id, slot)
        with job_registration_guard(store, job_id):
            fenced = FencedEnginePersistence(store, _claim_global(store, job_id, owner_id, slot))
            with fenced.transaction() as uow:
                started = activate_trigger(
                    job, uow, trigger_id=trigger_id, config_json=GLOBAL_CONFIG_JSON, now=slot,
                )
        if started:
            complete_trigger(job, fenced, trigger_id=trigger_id, now=slot)
            completed += 1
    return completed

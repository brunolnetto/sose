"""Falsify tampered completed-trigger history before recurring OD-5a effects.

The same persistence-backed contract runs on SQLite and real PostgreSQL.
Never accept final-state equality as a substitute for causal ID consistency.
"""
from __future__ import annotations

import os
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest

from sose.core.identity import deterministic_id
from sose.organizational.model_spec import EvidenceClass, ModelSpec
from sose.organizational.od5a_admission import OD5aChargedCost, compile_od5a_arms
from sose.organizational.od5a_mechanisms import OD5aWorkItem
from sose.organizational.od5a_durable import od5a_durable_state, od5a_durable_events
from sose.organizational.od5a_recurring import (
    initialize_od5a_recurring_job,
    claim_od5a_recurring_tick,
    execute_od5a_claimed_tick,
    run_od5a_recurring_tick, _read_job,
)
from sose.persistence.postgres import PostgresPersistence
from sose.persistence.sqlite import SQLitePersistence

NOW = datetime.now(timezone.utc)
LEASE = timedelta(minutes=15)
JOB = "org:trigger-history"
DSN = os.environ.get("SOSE_TEST_POSTGRES_DSN")


@pytest.fixture(params=["sqlite", "postgres"])
def store(request, tmp_path):
    if request.param == "postgres":
        if not DSN:
            pytest.skip("real PostgreSQL DSN required")
        backend = PostgresPersistence(
            DSN, namespace="od5a_history_" + uuid4().hex[:16],
        )
    else:
        backend = SQLitePersistence(tmp_path / "od5a-trigger-history.sqlite")
    with backend as persistence:
        spec = ModelSpec(
            stations={"a": {}, "b": {}, "c": {}},
            routing={"dependency_edges": [["a", "b"], ["b", "c"]]},
            demand={"arrival_process": "exogenous"},
            parameters={
                "arrival_rate": 0.5, "execution_slots": 1,
                "coordination_latency": 0.1, "service_cv": 0.5,
            },
            parameter_evidence={
                key: EvidenceClass.ASSUMED for key in
                ("arrival_rate", "execution_slots", "coordination_latency", "service_cv")
            },
        )
        arm = compile_od5a_arms(
            baseline=spec, expanded_capacity=2,
            removed_dependency_edges=(("a", "b"),),
            reduced_coordination_latency=0.05,
            declared_costs={
                key: OD5aChargedCost(8, 1, 1, 1)
                for key in ("capacity_only", "topology_only",
                            "coordination_only", "structural_combined")
            },
        )[0]
        initialize_od5a_recurring_job(
            persistence, job_id=JOB, arm=arm,
            items=(OD5aWorkItem(
                "one", 0.0, (("a", 0.2), ("b", 0.2), ("c", 0.1)),
            ),),
        )
        yield persistence


def _trigger(step: int) -> str:
    return deterministic_id("od5a-recurring-tick", JOB, step)


def _facts(store):
    return (
        od5a_durable_state(store, job_id=JOB),
        od5a_durable_events(store, job_id=JOB),
        store.resource_reservations(),
    )


def _advance_first(store) -> None:
    result = run_od5a_recurring_tick(
        store, job_id=JOB, owner_id="worker:A",
        now=NOW, lease_duration=LEASE, trigger_id=_trigger(1),
    )
    assert result.action == "advanced"
    assert store.job_state(JOB).run_count == 1


@pytest.mark.parametrize("forged", ["forged", "cleared"])
def test_zero_step_rejects_forged_completed_trigger_before_claim(store, forged):
    with store.transaction() as uow:
        job = uow.get_job_state(JOB)
        uow.save_job_state(replace(
            job, last_completed_trigger_id=("fabricated" if forged == "forged" else ""),
        ))
    before = _facts(store)
    with pytest.raises(ValueError, match="checkpoint contradicts"):
        claim_od5a_recurring_tick(
            store, job_id=JOB, owner_id="worker:B",
            now=NOW, lease_duration=LEASE, trigger_id=_trigger(1),
        )
    assert _facts(store) == before
    assert store.job_state(JOB).run_count == 0


@pytest.mark.parametrize("forged", ["replaced", "cleared"])
def test_idle_completed_history_cannot_be_laundered_by_replay(store, forged):
    _advance_first(store)
    with store.transaction() as uow:
        job = uow.get_job_state(JOB)
        value = "fabricated" if forged == "replaced" else None
        uow.save_job_state(replace(job, last_completed_trigger_id=value))
        if forged == "replaced":
            lease = uow.get_entity("od5a_runner_lease", JOB)
            lease.attributes["trigger_id"] = value
            uow.save_entity(lease)
    before = _facts(store)
    # The shared reader must reject corruption even when a later lease check
    # would happen to reject this specific call by another route.
    with store.transaction() as uow:
        with pytest.raises(ValueError, match="checkpoint contradicts"):
            _read_job(uow, JOB)
    if forged == "replaced":
        # Forging both sides of the released-lease comparison previously
        # authorized a false idempotent replay without validating run_count.
        with pytest.raises(ValueError, match="checkpoint contradicts"):
            run_od5a_recurring_tick(
                store, job_id=JOB, owner_id="worker:B", now=NOW,
                lease_duration=LEASE, trigger_id="fabricated",
            )
    assert _facts(store) == before


@pytest.mark.parametrize("forged", ["replaced", "cleared"])
def test_active_claim_rejects_corrupt_completed_history_before_simpy(store, forged):
    _advance_first(store)
    claim = claim_od5a_recurring_tick(
        store, job_id=JOB, owner_id="worker:B",
        now=NOW, lease_duration=LEASE, trigger_id=_trigger(2),
    )
    assert claim is not None
    with store.transaction() as uow:
        job = uow.get_job_state(JOB)
        uow.save_job_state(replace(
            job, last_completed_trigger_id=("fabricated" if forged == "replaced" else None),
        ))
    before = _facts(store)
    with pytest.raises(ValueError, match="checkpoint contradicts"):
        execute_od5a_claimed_tick(store, claim=claim, now=NOW)
    assert _facts(store) == before
    assert store.job_state(JOB).run_count == 1

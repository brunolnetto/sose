"""TDD contracts for #490: real recurring SimPy source + durable boundary phases.

Compare the independent #488 reducer/reference orchestration with the new
fenced source-boundary adapter after EVERY durable occurrence, not just
terminal throughput. PostgreSQL cases run on actual separated namespaces.
"""
from __future__ import annotations

import os
from contextlib import ExitStack
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest

from sose.core.identity import deterministic_id
from sose.organizational.model_spec import EvidenceClass, ModelSpec
from sose.organizational.od5a_admission import OD5aChargedCost, compile_od5a_arms
from sose.organizational.od5a_durable import initialize_od5a_durable
from sose.organizational.od5a_mechanisms import OD5aWorkItem
from sose.organizational.od5a_recurring import initialize_od5a_recurring_job
from sose.organizational.od5a_reference_runner import advance_od5a_reference_occurrence
from sose.organizational.od5a_integrated import advance_od5a_integrated_occurrence
from sose.persistence.sqlite import SQLitePersistence
from sose.persistence.postgres import PostgresPersistence


NOW = datetime(2030, 1, 1, tzinfo=timezone.utc)
LEASE = timedelta(minutes=5)
PHYSICAL = frozenset({"od5a.execution.v1", "od5a.coordination.v1"})
PHASES = ("published", "claimed", "acknowledged", "materialized", "certified")
DSN = os.getenv("SOSE_TEST_POSTGRES_DSN")


def _arm():
    model = ModelSpec(
        stations={"a": {}, "b": {}, "c": {}},
        routing={"dependency_edges": [["a", "b"], ["b", "c"]]},
        demand={"arrival_process": "exogenous"},
        parameters={
            "arrival_rate": 0.8, "execution_slots": 1,
            "coordination_latency": 0.1, "service_cv": 0.5,
        },
        parameter_evidence={
            key: EvidenceClass.ASSUMED for key in
            ("arrival_rate", "execution_slots", "coordination_latency", "service_cv")
        },
    )
    return compile_od5a_arms(
        baseline=model, expanded_capacity=2,
        removed_dependency_edges=(("a", "b"),),
        reduced_coordination_latency=0.05,
        declared_costs={
            key: OD5aChargedCost(8, 1, 1, 1)
            for key in ("capacity_only", "topology_only",
                        "coordination_only", "structural_combined")
        },
    )[0]


def _items(job):
    return (OD5aWorkItem(
        "item:" + job, 0.0, (("a", 0.2), ("b", 0.3), ("c", 0.1)),
    ),)


def _truth(store, organizations):
    """Audit the physical/event DAG and every independent downstream boundary."""
    with store.transaction() as uow:
        events = tuple(sorted(
            (e for e in uow.events() if e.entity_id in organizations),
            key=lambda e: e.event_id,
        ))
        physical = tuple(e for e in events if e.name in PHYSICAL)
        deliveries = tuple(sorted(
            (d for d in uow.boundary_deliveries()
             if (m := uow.get_boundary_message(d.message_id)) is not None
             and m.correlation_id in organizations),
            key=lambda d: d.delivery_id,
        ))
        messages = tuple(uow.get_boundary_message(d.message_id) for d in deliveries)
        return (
            tuple(uow.get_entity("od5a_durable_pilot", j) for j in organizations),
            events,
            tuple(sorted(uow.resource_reservations(), key=lambda x: x.reservation_id)),
            messages, deliveries,
            tuple(uow.get_boundary_consumption(d.delivery_id) for d in deliveries),
            tuple(
                (m.message_id, uow.get_command(deterministic_id(
                    "od5a-physical-command", m.message_id,
                ))) for m in messages
            ),
            tuple(
                (e.event_id, uow.get_entity("od5a_physical_observation", e.event_id))
                for e in physical
            ),
            tuple(sorted(uow.business_effects(), key=lambda x: x.effect_id)),
        )


@pytest.mark.parametrize("adapter,organizations", [
    ("sqlite", ("org:A",)),
    ("sqlite", ("org:A", "org:B")),
    ("postgres", ("org:A",)),
])
def test_integrated_occurrences_match_reference_at_every_commit(
    tmp_path, adapter, organizations,
):
    if adapter == "postgres" and not DSN:
        pytest.skip("live PostgreSQL DSN required")
    with ExitStack() as stack:
        if adapter == "postgres":
            suffix = uuid4().hex[:12]
            oracle = stack.enter_context(PostgresPersistence(
                DSN, namespace="od5a_oracle_" + suffix,
            ))
            integrated = stack.enter_context(PostgresPersistence(
                DSN, namespace="od5a_integrated_" + suffix,
            ))
        else:
            oracle = stack.enter_context(SQLitePersistence(tmp_path / "oracle.sqlite"))
            integrated = stack.enter_context(SQLitePersistence(tmp_path / "integrated.sqlite"))
        arm = _arm()
        for job_id in organizations:
            initialize_od5a_durable(
                oracle, job_id=job_id, arm=arm, items=_items(job_id),
            )
            initialize_od5a_recurring_job(
                integrated, job_id=job_id, arm=arm, items=_items(job_id),
            )
        audit = {job_id: [] for job_id in organizations}
        completed = set()
        for _ in range(180):
            for job_id in organizations:
                if job_id in completed:
                    continue
                reference = advance_od5a_reference_occurrence(
                    oracle, job_id=job_id, owner_id="worker:" + job_id,
                    now=NOW, lease_duration=LEASE,
                )
                job = integrated.job_state(job_id)
                trigger = deterministic_id(
                    "od5a-recurring-tick", job_id, job.run_count + 1,
                )
                actual = advance_od5a_integrated_occurrence(
                    integrated, job_id=job_id, owner_id="worker:" + job_id,
                    now=NOW, lease_duration=LEASE,
                    source_trigger_id=trigger,
                )
                audit[job_id].append(actual.action)
                if reference.action == "complete":
                    assert actual.action in {"source_complete", "complete"}
                else:
                    assert actual.action == reference.action
                # The source has its own durable JobState, but all physical,
                # temporal, causal, transport and certification truth must
                # agree with the independently driven reference.
                assert _truth(integrated, organizations) == _truth(oracle, organizations)
                if actual.action == "complete":
                    completed.add(job_id)
            if len(completed) == len(organizations):
                break
        else:
            pytest.fail("integrated recurring adapter did not converge")
        for job_id in organizations:
            actions = audit[job_id]
            assert "source_advanced" in actions
            assert set(PHASES) <= set(actions)
            assert "complete" in actions
            with integrated.transaction() as uow:
                state = uow.get_job_state(job_id)
                source = uow.get_entity("od5a_durable_pilot", job_id)
                assert state.run_count == source.attributes["step"]
                assert state.next_tick == source.attributes["step"]
                assert state.active_trigger_id is None


def test_boundary_backlog_precedes_next_source_step(tmp_path):
    with SQLitePersistence(tmp_path / "integrated.sqlite") as store:
        job_id = "org:backlog"
        initialize_od5a_recurring_job(
            store, job_id=job_id, arm=_arm(), items=_items(job_id),
        )
        for _ in range(35):
            current = store.job_state(job_id)
            trigger = deterministic_id(
                "od5a-recurring-tick", job_id, current.run_count + 1,
            )
            step = advance_od5a_integrated_occurrence(
                store, job_id=job_id, owner_id="one",
                now=NOW, lease_duration=LEASE, source_trigger_id=trigger,
            )
            if step.action == "published":
                tick = store.job_state(job_id).run_count
                later = advance_od5a_integrated_occurrence(
                    store, job_id=job_id, owner_id="one",
                    now=NOW, lease_duration=LEASE,
                    source_trigger_id=trigger,
                )
                assert later.action == "claimed"
                assert store.job_state(job_id).run_count == tick
                return
        pytest.fail("no physical publication reached after source steps")

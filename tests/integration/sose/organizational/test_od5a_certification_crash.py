"""OD-5a #490 evidence: process-death durability and independent causal oracle.

These are genuine spawned worker OS processes with os._exit after the durable
COMMIT. A new process rebuilds from SQLite, not SimPy memory or live handles.
"""
from __future__ import annotations

from collections import Counter
from datetime import datetime, timedelta, timezone
from multiprocessing import get_context
import os

import pytest

from sose.core.identity import deterministic_id
from sose.organizational.model_spec import EvidenceClass, ModelSpec
from sose.organizational.od5a_admission import OD5aChargedCost, compile_od5a_arms
from sose.organizational.od5a_durable import initialize_od5a_durable
from sose.organizational.od5a_integrated import advance_od5a_integrated_occurrence
from sose.organizational.od5a_mechanisms import OD5aWorkItem
from sose.organizational.od5a_recurring import initialize_od5a_recurring_job
from sose.organizational.od5a_reference_runner import advance_od5a_reference_occurrence
from sose.persistence.sqlite import SQLitePersistence
from tests.support.causal_certificate import capture_causal_certificate

NOW = datetime(2030, 1, 1, tzinfo=timezone.utc)
LEASE = timedelta(seconds=30)
BOUNDARY_PHASES = frozenset({
    "published", "claimed", "acknowledged", "materialized", "certified",
})


def _arm():
    baseline = ModelSpec(
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
        baseline=baseline, expanded_capacity=2,
        removed_dependency_edges=(("a", "b"),),
        reduced_coordination_latency=0.05,
        declared_costs={
            key: OD5aChargedCost(8, 1, 1, 1)
            for key in ("capacity_only", "topology_only",
                        "coordination_only", "structural_combined")
        },
    )[0]


def _items(organization):
    return (OD5aWorkItem(
        "item:" + organization, 0.0,
        (("a", 0.2), ("b", 0.3), ("c", 0.1)),
    ),)


def _certificate(store, organizations):
    return capture_causal_certificate(
        store, organizations=organizations,
        source_type="od5a_durable_pilot",
        observation_type="od5a_physical_observation",
        physical_event_names=frozenset({
            "od5a.execution.v1", "od5a.coordination.v1",
        }),
        command_identity_namespace="od5a-physical-command",
    )


def _next_trigger(store, org):
    return deterministic_id(
        "od5a-recurring-tick", org, store.job_state(org).run_count + 1,
    )


def _integrated(store, org, owner, now):
    return advance_od5a_integrated_occurrence(
        store, job_id=org, owner_id=owner, now=now,
        lease_duration=LEASE, source_trigger_id=_next_trigger(store, org),
    )


def _kill_before_sqlite_commit(path):
    # Real OS death before any SOSE durable boundary invocation.
    with SQLitePersistence(path) as store:
        assert store.job_states()
        os._exit(86)


def _kill_after_sqlite_commits(path, org, owner, now, actions):
    with SQLitePersistence(path) as store:
        for expected in actions:
            actual = _integrated(store, org, owner, now)
            if actual.action != expected:
                raise RuntimeError(
                    f"committed {actual.action}, expected {expected}"
                )
        # The caller sees NO response, but all UoWs above have committed.
        os._exit(87)


def _exitcode(target, args, expected):
    ctx = get_context("spawn")
    worker = ctx.Process(target=target, args=args)
    worker.start()
    worker.join(timeout=45)
    try:
        if worker.is_alive():
            pytest.fail("spawned recurring worker did not terminate")
        assert worker.exitcode == expected
    finally:
        if worker.is_alive():
            worker.kill()
            worker.join()


def test_integrated_real_os_deaths_before_after_every_boundary_commit(tmp_path):
    """Falsify every phase, with a distinct process between durable commits.

    A killed claim owner cannot ACK on recovery. After a claim dies, the next
    owner reclaims after expiry, then performs ACK using its newly fenced lease.
    Compare the *entire persisted causal record* at each crash boundary.
    """
    org = "org:kill"
    control, restarted = tmp_path / "oracle.db", tmp_path / "killed.db"
    with SQLitePersistence(control) as source:
        initialize_od5a_durable(
            source, job_id=org, arm=_arm(), items=_items(org),
        )
    with SQLitePersistence(restarted) as source:
        initialize_od5a_recurring_job(
            source, job_id=org, arm=_arm(), items=_items(org),
        )
    terminated_before = set()
    actions = []
    previous = None
    for attempt in range(110):
        owner = f"owner:{attempt}"
        now = NOW + (LEASE + timedelta(seconds=1)) * attempt
        with SQLitePersistence(control) as oracle:
            first = advance_od5a_reference_occurrence(
                oracle, job_id=org, owner_id=owner, now=now,
                lease_duration=LEASE,
            )
            if first.action == "complete":
                with SQLitePersistence(restarted) as recovered:
                    assert _integrated(recovered, org, owner, now).action == "complete"
                    assert _certificate(recovered, (org,)) == _certificate(oracle, (org,))
                break
            assert first.action not in {"idle", "blocked"}
            expected = [first.action]
            if previous == "claimed":
                # Expired claim is RECLAIMED by new owner; that owner commits
                # the ACK before its process is killed after that second UoW.
                assert first.action == "claimed"
                second = advance_od5a_reference_occurrence(
                    oracle, job_id=org, owner_id=owner, now=now,
                    lease_duration=LEASE,
                )
                assert second.action == "acknowledged"
                expected.append(second.action)
            previous = expected[-1]
            reference = _certificate(oracle, (org,))
        with SQLitePersistence(restarted) as store:
            before = _certificate(store, (org,))
        for action in expected:
            if action not in terminated_before:
                _exitcode(_kill_before_sqlite_commit, (str(restarted),), 86)
                with SQLitePersistence(restarted) as unchanged:
                    assert _certificate(unchanged, (org,)) == before
                terminated_before.add(action)
        _exitcode(
            _kill_after_sqlite_commits,
            (str(restarted), org, owner, now, tuple(expected)), 87,
        )
        with SQLitePersistence(restarted) as recovered:
            assert _certificate(recovered, (org,)) == reference
            job = recovered.job_state(org)
            assert job.run_count == reference.sources[0].attributes["step"]
            assert job.next_tick == reference.sources[0].attributes["step"]
            assert job.logical_time == reference.sources[0].updated_at
            assert len({e.event_id for e in reference.events}) == len(reference.events)
        actions.extend(expected)
    else:
        pytest.fail("OD-5a killed-worker causal chain did not converge")

    counts = Counter(actions)
    assert set(BOUNDARY_PHASES) <= set(counts)
    assert set(BOUNDARY_PHASES) <= terminated_before
    assert all(counts[phase] >= 4 for phase in BOUNDARY_PHASES)
    assert counts["source_advanced"] >= 5
    with SQLitePersistence(restarted) as recovered, SQLitePersistence(control) as oracle:
        assert _certificate(recovered, (org,)) == _certificate(oracle, (org,))
        assert recovered.job_state(org).active_trigger_id is None
        assert recovered.resource_reservations() == ()


def test_certificate_falsifies_unexpected_well_formed_effect(tmp_path):
    """Correct expected receipts must not mask an additional bogus receipt."""
    from sose.composition.effects import BusinessEffectApplied

    path = tmp_path / "extra-effect.db"
    org = "org:extra-effect"
    with SQLitePersistence(path) as store:
        initialize_od5a_recurring_job(
            store, job_id=org, arm=_arm(), items=_items(org),
        )
        before = _certificate(store, (org,))
        with store.transaction() as uow:
            # The generic OLTP layer can persist malformed domain history;
            # this injected excess certificate must be visible to the
            # *auditor* even though no expected boundary message points to it.
            uow.save_business_effect(BusinessEffectApplied.from_applied(
                effect_id="forged-extra-effect",
                boundary_message_id="forged-message",
                correlation_id=org,
                entity_type="od5a_physical_observation",
                entity_id="forged-observation",
                terminal_state="observed",
                entity_version=1,
                completed_at=NOW,
            ))
        after = _certificate(store, (org,))
        assert before.effects == after.effects == ()
        assert before.all_effects == ()
        assert len(after.all_effects) == 1
        assert before != after

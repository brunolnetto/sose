"""OD-5a #490 evidence: process-death durability and independent causal oracle.

These are genuine spawned worker OS processes with os._exit after the durable
COMMIT. A new process rebuilds from SQLite, not SimPy memory or live handles.
"""
from __future__ import annotations

from collections import Counter
from datetime import datetime, timedelta, timezone
from multiprocessing import get_context
import os
import sqlite3

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


def _kill_inside_phase_before_commit(path, org, owner, now, action, prefix):
    """Abort a live UoW after staging THIS phase's mutation, before COMMIT.

    A simple worker death before calling the adapter would be vacuous.
    Replace the specific UoW write primitive in the child process; the
    operation must actually reach that durable phase before os._exit(86).
    """
    from sose.persistence.memory import MemoryUnitOfWork

    writes = {
        "source_advanced": "append_event",
        "published": "save_boundary_message",
        "claimed": "save_boundary_delivery",
        "acknowledged": "save_boundary_consumption",
        "materialized": "save_entity",
        "certified": "save_business_effect",
    }
    with SQLitePersistence(path) as store:
        for previous in prefix:
            actual = _integrated(store, org, owner, now)
            if actual.action != previous:
                raise RuntimeError(
                    f"phase setup committed {actual.action} instead of {previous}"
                )
        method = writes[action]
        original = getattr(MemoryUnitOfWork, method)

        def abort_staged_write(uow, *args, **kwargs):
            value = original(uow, *args, **kwargs)
            if action == "materialized" and args[0].entity_type != "od5a_physical_observation":
                return value
            # The mutation is in the uncommitted UnitOfWork. A real abrupt
            # process death now discards it; recovery must reconstruct the
            # exact prior committed causality without any phantom receipt.
            os._exit(86)

        setattr(MemoryUnitOfWork, method, abort_staged_write)
        # Exit code 86 is possible ONLY after the requested phase actually
        # reaches its staging write. A different phase or early no-op fails.
        _integrated(store, org, owner, now)
        os._exit(84)


def _clone_sqlite(source, target):
    # SQLite backup API captures all committed WAL pages consistently; plain
    # file copying can omit WAL records and produce a false crash result.
    with sqlite3.connect(source) as original:
        with sqlite3.connect(target) as replica:
            original.backup(replica)


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
            intermediate = _certificate(oracle, (org,))
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
        for offset, action in enumerate(expected):
            if action not in terminated_before:
                probe = tmp_path / f"precommit-{attempt}-{action}.sqlite"
                _clone_sqlite(restarted, probe)
                prefix = tuple(expected[:offset])
                _exitcode(
                    _kill_inside_phase_before_commit,
                    (str(probe), org, owner, now, action, prefix), 86,
                )
                with SQLitePersistence(probe) as aborted:
                    # ACK's pre-commit kill first RECLAIMS and commits the
                    # new owner's claim; the expected rollback point is
                    # therefore the oracle state after that preceding claim.
                    assert _certificate(aborted, (org,)) == (
                        intermediate if offset else before
                    )
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


def test_certificate_falsifies_unpaired_organization_message(tmp_path):
    """An orphan publication must be visible even without a delivery row."""
    from sose.composition.model import BoundaryMessage

    org = "org:orphan-message"
    with SQLitePersistence(tmp_path / "orphan.db") as store:
        initialize_od5a_recurring_job(
            store, job_id=org, arm=_arm(), items=_items(org),
        )
        original = _certificate(store, (org,))
        message = BoundaryMessage.create(
            contract_name="od5a.orphan_probe", contract_version=1,
            source_domain="od5a_durable", source_identity=org,
            destination_domain="od5a_command_staging",
            occurrence_key="unpaired", correlation_id=org,
            causation_id=None, produced_at=NOW,
            payload={"test": "unpaired-publication"},
        )
        with store.transaction() as uow:
            uow.save_boundary_message(message)
        after = _certificate(store, (org,))
        assert original.messages == ()
        assert after.deliveries == ()
        assert after.messages == (message,)
        assert original != after


def test_certificate_falsifies_unexpected_organization_command(tmp_path):
    """An excess executable intent must be visible even if no message points to it."""
    from sose.core.events import Command

    org = "org:extra-command"
    with SQLitePersistence(tmp_path / "orphan-command.db") as store:
        initialize_od5a_recurring_job(
            store, job_id=org, arm=_arm(), items=_items(org),
        )
        original = _certificate(store, (org,))
        forged = Command(
            command_id="forged-od5a-command",
            name="od5a.stage_physical_completion.v1",
            entity_type="od5a_physical_observation",
            entity_id="forged-observation",
            due_at=NOW,
            causation_id="forged-message",
            correlation_id=org,
        )
        with store.transaction() as uow:
            uow.save_command(forged)
        after = _certificate(store, (org,))
        assert original.commands == after.commands == ()
        assert original.all_commands == ()
        assert after.all_commands == (forged,)
        assert original != after


def test_certificate_falsifies_extra_event_with_wrong_source_entity(tmp_path):
    """Correlation still identifies a rogue event with an incorrect entity ID."""
    from sose.core.events import DomainEvent

    org = "org:wrong-event"
    with SQLitePersistence(tmp_path / "wrong-event.db") as store:
        initialize_od5a_recurring_job(
            store, job_id=org, arm=_arm(), items=_items(org),
        )
        before = _certificate(store, (org,))
        rogue = DomainEvent(
            event_id="rogue-event",
            name="od5a.execution.v1",
            entity_type="od5a_durable_pilot",
            entity_id="wrong-physical-entity",
            occurred_at=NOW,
            correlation_id=org,
        )
        with store.transaction() as uow:
            uow.append_event(rogue)
        after = _certificate(store, (org,))
        assert before.events == ()
        assert after.events == (rogue,)
        assert before != after


def test_certificate_falsifies_extra_correlated_observation(tmp_path):
    """An extra terminal observation without a source DomainEvent must be visible."""
    from sose.domain.entity import Entity

    org = "org:extra-observation"
    with SQLitePersistence(tmp_path / "extra-observation.db") as store:
        initialize_od5a_recurring_job(
            store, job_id=org, arm=_arm(), items=_items(org),
        )
        before = _certificate(store, (org,))
        rogue = Entity(
            id="forged-observation",
            entity_type="od5a_physical_observation",
            state="observed",
            attributes={"correlation_id": org, "source_event_id": "nonexistent"},
        )
        with store.transaction() as uow:
            uow.save_entity(rogue)
        after = _certificate(store, (org,))
        assert before.observations == after.observations == ()
        assert before.all_observations == ()
        assert after.all_observations == (rogue,)
        assert before != after


def test_certificate_falsifies_extra_receipt_without_matching_delivery(tmp_path):
    """A valid org message can have an unpaired ACK row with a forged delivery."""
    from sose.composition.model import BoundaryMessage, BoundaryConsumption

    org = "org:extra-consumption"
    with SQLitePersistence(tmp_path / "extra-consumption.db") as store:
        initialize_od5a_recurring_job(
            store, job_id=org, arm=_arm(), items=_items(org),
        )
        message = BoundaryMessage.create(
            contract_name="od5a.physical_completion", contract_version=1,
            source_domain="od5a_durable", source_identity=org,
            destination_domain="od5a_command_staging", occurrence_key="fake",
            correlation_id=org, causation_id=None,
            produced_at=NOW, payload={"test": "extra-ack"},
        )
        with store.transaction() as uow:
            uow.save_boundary_message(message)
        before = _certificate(store, (org,))
        rogue = BoundaryConsumption(
            consumption_id="rogue-consumption",
            delivery_id="nonexistent-delivery",
            message_id=message.message_id,
            destination_domain="od5a_command_staging",
            contract_name=message.contract_name,
            contract_version=1,
            consumer_effect_id="rogue-effect",
            consumed_at=NOW,
        )
        with store.transaction() as uow:
            uow.save_boundary_consumption(rogue)
        after = _certificate(store, (org,))
        assert before.consumptions == after.consumptions == ()
        assert before.all_consumptions == ()
        assert after.all_consumptions == (rogue,)
        assert before != after

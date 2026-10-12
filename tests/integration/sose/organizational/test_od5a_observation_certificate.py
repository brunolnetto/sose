"""OD-5a event-keyed authoritative observation and certificate recovery falsifiers.

The physical event, boundary ACK, staged Command, materialized destination
Entity and immutable BusinessEffectApplied are five different durable facts.
Not confirmatory OD-5a scientific evidence.
"""
from __future__ import annotations

from dataclasses import replace
from datetime import timedelta
from multiprocessing import get_context

import pytest

from sose.composition.effects import BusinessEffectService
from sose.domain.entity import Entity
from sose.organizational.model_spec import EvidenceClass, ModelSpec
from sose.organizational.od5a_admission import OD5aChargedCost, compile_od5a_arms
from sose.organizational.od5a_boundary import (
    publish_od5a_physical_completion,
    claim_od5a_physical_completion,
    consume_od5a_physical_completion,
)
from sose.organizational.od5a_durable import (
    initialize_od5a_durable, advance_od5a_durable, od5a_durable_events,
)
from sose.organizational.od5a_mechanisms import OD5aWorkItem
from sose.organizational.od5a_observation import (
    apply_od5a_physical_observation,
    certify_od5a_physical_observation,
)
from sose.persistence.memory import MemoryUnitOfWork
from sose.persistence.sqlite import SQLitePersistence


def _arms():
    baseline = ModelSpec(
        stations={"a": {}, "b": {}, "c": {}},
        routing={"dependency_edges": [["a", "b"], ["b", "c"]]},
        demand={"arrival_process": "exogenous"},
        parameters={
            "arrival_rate": 0.8, "execution_slots": 1,
            "coordination_latency": 0.1, "service_cv": 0.5,
        },
        parameter_evidence={
            k: EvidenceClass.ASSUMED for k in (
                "arrival_rate", "execution_slots", "coordination_latency", "service_cv",
            )
        },
    )
    return {x.arm_id: x for x in compile_od5a_arms(
        baseline=baseline, expanded_capacity=2,
        removed_dependency_edges=(("a", "b"),),
        reduced_coordination_latency=0.05,
        declared_costs={
            k: OD5aChargedCost(8, 1, 1, 1)
            for k in ("capacity_only", "topology_only",
                      "coordination_only", "structural_combined")
        },
    )}


def _stage(path, *, job_id="org:alpha", arm="baseline", kind="execution"):
    with SQLitePersistence(path) as store:
        initialize_od5a_durable(
            store, job_id=job_id, arm=_arms()[arm],
            items=(OD5aWorkItem(
                item_id="item:" + job_id, arrival_at=0.0,
                service_by_station=(("a", 0.2), ("b", 0.3), ("c", 0.1)),
            ),),
        )
        for _ in range(30):
            events = od5a_durable_events(store, job_id=job_id)
            match = next((e for e in events if e.payload["kind"] == kind), None)
            if match is not None:
                break
            assert advance_od5a_durable(store, job_id=job_id)
        else:
            raise AssertionError("physical event not committed")
        published = publish_od5a_physical_completion(
            store, job_id=job_id, event_id=match.event_id,
        )
        now = match.occurred_at + timedelta(seconds=1)
        lease = claim_od5a_physical_completion(
            store, job_id=job_id, owner_id="owner:" + job_id,
            now=now, lease_duration=timedelta(seconds=30),
        )
        assert lease is not None
        consumed = consume_od5a_physical_completion(store, lease=lease, now=now)
        assert consumed.message_id == published.message_id
        return match, consumed.consumer_effect_id


def _snapshot(path, job_id, event_id, effect_id):
    with SQLitePersistence(path) as store:
        with store.transaction() as uow:
            return (
                uow.get_entity("od5a_physical_observation", event_id),
                uow.get_command(effect_id),
                uow.get_business_effect(effect_id),
                tuple(d for d in uow.boundary_deliveries()
                      if (m := uow.get_boundary_message(d.message_id)) is not None
                      and m.correlation_id == job_id),
            )


@pytest.mark.parametrize("arm", [
    "baseline", "capacity_only", "topology_only",
    "coordination_only", "structural_combined",
])
def test_all_arms_ack_apply_certificate_are_distinct_durable_steps(tmp_path, arm):
    path = tmp_path / "observation.db"
    event, effect_id = _stage(path, arm=arm)
    observation, pending, proof, _ = _snapshot(path, "org:alpha", event.event_id, effect_id)
    assert observation is None and pending is not None and proof is None

    with SQLitePersistence(path) as store:
        with pytest.raises(ValueError, match="terminal"):
            BusinessEffectService(store).complete(
                effect_id=effect_id, completed_at=event.occurred_at + timedelta(seconds=2),
            )
        with pytest.raises(ValueError, match="observation"):
            certify_od5a_physical_observation(
                store, effect_id=effect_id,
                completed_at=event.occurred_at + timedelta(seconds=2),
            )

    applied_at = event.occurred_at + timedelta(seconds=2)
    with SQLitePersistence(path) as store:
        observation = apply_od5a_physical_observation(
            store, effect_id=effect_id, applied_at=applied_at,
        )
        assert observation.id == event.event_id
        assert observation.entity_type == "od5a_physical_observation"
        assert observation.state == "observed" and observation.version == 1
        assert observation.attributes["source_event_id"] == event.event_id
        assert observation.attributes["resource"] == event.payload["resource"]
        assert observation.attributes["model_spec_hash"] == event.payload["model_spec_hash"]
        assert observation.attributes["start_us"] <= observation.attributes["end_us"]
        assert apply_od5a_physical_observation(
            store, effect_id=effect_id, applied_at=applied_at + timedelta(seconds=1),
        ) == observation
        assert store.command(effect_id) is not None
        assert store.business_effect(effect_id) is None

    with SQLitePersistence(path) as store:
        completed_at = event.occurred_at + timedelta(seconds=4)
        certificate = certify_od5a_physical_observation(
            store, effect_id=effect_id, completed_at=completed_at,
        )
        assert certificate.entity_type == observation.entity_type
        assert certificate.entity_id == observation.id
        assert certificate.entity_version == 1
        assert certificate.terminal_state == "observed"
        assert certificate.correlation_id == "org:alpha"
        assert store.command(effect_id) is None
        assert store.business_effect(effect_id) == certificate
        assert certify_od5a_physical_observation(
            store, effect_id=effect_id, completed_at=completed_at + timedelta(seconds=1),
        ) == certificate
        assert apply_od5a_physical_observation(
            store, effect_id=effect_id, applied_at=applied_at,
        ) == observation

    final, cmd, proof, _ = _snapshot(path, "org:alpha", event.event_id, effect_id)
    assert final == observation and cmd is None and proof == certificate


@pytest.mark.parametrize("kind", ["execution", "coordination"])
def test_both_physical_event_kinds_are_materialized_independently(tmp_path, kind):
    path = tmp_path / "physical-kinds.db"
    event, effect_id = _stage(path, kind=kind)
    with SQLitePersistence(path) as store:
        obs = apply_od5a_physical_observation(
            store, effect_id=effect_id, applied_at=event.occurred_at + timedelta(seconds=2),
        )
        assert obs.attributes["kind"] == kind
        cert = certify_od5a_physical_observation(
            store, effect_id=effect_id, completed_at=event.occurred_at + timedelta(seconds=2),
        )
        assert cert.entity_id == event.event_id


@pytest.mark.parametrize("tamper", [
    "source_event_id", "model_spec_hash", "config_epoch",
    "resource", "start_us", "end_us", "correlation_id",
])
def test_tampered_destination_cannot_be_certified(tmp_path, tamper):
    path = tmp_path / "tampered-observation.db"
    event, effect_id = _stage(path)
    with SQLitePersistence(path) as store:
        apply_od5a_physical_observation(
            store, effect_id=effect_id, applied_at=event.occurred_at + timedelta(seconds=2),
        )
        with store.transaction() as uow:
            obs = uow.get_entity("od5a_physical_observation", event.event_id)
            attrs = {**obs.attributes, tamper: "forged" if tamper not in
                     {"config_epoch", "start_us", "end_us"} else 999}
            uow.save_entity(replace(obs, attributes=attrs))
        with pytest.raises(ValueError, match="observation"):
            certify_od5a_physical_observation(
                store, effect_id=effect_id, completed_at=event.occurred_at + timedelta(seconds=2),
            )
        assert store.command(effect_id) is not None
        assert store.business_effect(effect_id) is None


@pytest.mark.parametrize("tamper", [
    "name", "entity_type", "entity_id", "correlation_id",
    "causation_id", "payload",
])
def test_wrong_command_cannot_materialize_or_certify(tmp_path, tamper):
    path = tmp_path / "wrong-command.db"
    event, effect_id = _stage(path)
    with SQLitePersistence(path) as store:
        with store.transaction() as uow:
            cmd = uow.get_command(effect_id)
            malicious = {
                "name": "composition.deliver_shipment",
                "entity_type": "shipment",
                "entity_id": "org:foreign",
                "correlation_id": "org:foreign",
                "causation_id": "forged-message",
                "payload": {"event_id": "forged"},
            }
            uow.save_command(replace(cmd, **{tamper: malicious[tamper]}))
        with pytest.raises(ValueError):
            apply_od5a_physical_observation(
                store, effect_id=effect_id, applied_at=event.occurred_at + timedelta(seconds=2),
            )
        assert store.entity("od5a_physical_observation", event.event_id) is None
        assert store.business_effect(effect_id) is None


def test_preexisting_wrong_version_observation_is_never_overwritten(tmp_path):
    path = tmp_path / "wrong-version.db"
    event, effect_id = _stage(path)
    with SQLitePersistence(path) as store:
        with store.transaction() as uow:
            uow.save_entity(Entity(
                id=event.event_id, entity_type="od5a_physical_observation",
                state="observed", version=0,
                attributes={"source_event_id": event.event_id},
            ))
        with pytest.raises(ValueError, match="observation"):
            apply_od5a_physical_observation(
                store, effect_id=effect_id, applied_at=event.occurred_at + timedelta(seconds=2),
            )
        assert store.command(effect_id) is not None
        assert store.business_effect(effect_id) is None


def test_certificate_commit_fault_rolls_back_certificate_and_command_deletion(
    tmp_path, monkeypatch,
):
    path = tmp_path / "rollback.db"
    event, effect_id = _stage(path)
    with SQLitePersistence(path) as store:
        apply_od5a_physical_observation(
            store, effect_id=effect_id, applied_at=event.occurred_at + timedelta(seconds=2),
        )
        original = MemoryUnitOfWork.save_business_effect

        def write_then_fail(self, value):
            original(self, value)
            raise RuntimeError("kill before authoritative COMMIT")

        with monkeypatch.context() as patch:
            patch.setattr(MemoryUnitOfWork, "save_business_effect", write_then_fail)
            with pytest.raises(RuntimeError, match="before authoritative COMMIT"):
                certify_od5a_physical_observation(
                    store, effect_id=effect_id, completed_at=event.occurred_at + timedelta(seconds=2),
                )
        assert store.business_effect(effect_id) is None
        assert store.command(effect_id) is not None
        certificate = certify_od5a_physical_observation(
            store, effect_id=effect_id, completed_at=event.occurred_at + timedelta(seconds=2),
        )
        assert store.business_effect(effect_id) == certificate
        assert store.command(effect_id) is None


def _death_after_commit(path: str, effect_id: str, when: str, at):
    import os
    with SQLitePersistence(path) as store:
        if when == "apply":
            apply_od5a_physical_observation(
                store, effect_id=effect_id, applied_at=at,
            )
        else:
            certify_od5a_physical_observation(
                store, effect_id=effect_id, completed_at=at,
            )
        os._exit(97)


@pytest.mark.parametrize("death_window", ["apply", "certify"])
def test_real_os_worker_death_after_commit_recovers_exactly_once(tmp_path, death_window):
    interrupted = tmp_path / "interrupted.db"
    uninterrupted = tmp_path / "control.db"
    event, effect_id = _stage(interrupted)
    control_event, control_effect = _stage(uninterrupted)
    assert event == control_event and effect_id == control_effect
    at = event.occurred_at + timedelta(seconds=2)
    with SQLitePersistence(uninterrupted) as store:
        apply_od5a_physical_observation(store, effect_id=effect_id, applied_at=at)
        certify_od5a_physical_observation(store, effect_id=effect_id, completed_at=at)
    if death_window == "certify":
        with SQLitePersistence(interrupted) as store:
            apply_od5a_physical_observation(store, effect_id=effect_id, applied_at=at)
    worker = get_context("spawn").Process(
        target=_death_after_commit,
        args=(str(interrupted), effect_id, death_window, at),
    )
    worker.start()
    worker.join(timeout=30)
    if worker.is_alive():
        worker.kill()
        worker.join()
        pytest.fail("dead worker did not terminate")
    assert worker.exitcode == 97
    with SQLitePersistence(interrupted) as store:
        apply_od5a_physical_observation(store, effect_id=effect_id, applied_at=at)
        certify_od5a_physical_observation(store, effect_id=effect_id, completed_at=at)
    assert _snapshot(interrupted, "org:alpha", event.event_id, effect_id) == (
        _snapshot(uninterrupted, "org:alpha", event.event_id, effect_id)
    )


def test_independent_organization_certification_does_not_consume_foreign_command(tmp_path):
    path = tmp_path / "independent.db"
    a, ca = _stage(path, job_id="org:a", arm="baseline")
    b, cb = _stage(path, job_id="org:b", arm="capacity_only")
    with SQLitePersistence(path) as store:
        apply_od5a_physical_observation(store, effect_id=cb, applied_at=b.occurred_at + timedelta(seconds=2))
        certify_od5a_physical_observation(store, effect_id=cb, completed_at=b.occurred_at + timedelta(seconds=2))
        assert store.command(ca) is not None
        assert store.business_effect(ca) is None
        apply_od5a_physical_observation(store, effect_id=ca, applied_at=a.occurred_at + timedelta(seconds=2))
        certify_od5a_physical_observation(store, effect_id=ca, completed_at=a.occurred_at + timedelta(seconds=2))
        assert store.business_effect(cb).correlation_id == "org:b"
        assert store.business_effect(ca).correlation_id == "org:a"
        assert store.command(ca) is None and store.command(cb) is None


def _die_during_uncommitted_write(path: str, effect_id: str, at, stage: str):
    """Kill the OS process *after* a UoW mutation but before its COMMIT."""
    import os

    with SQLitePersistence(path) as store:
        if stage == "apply":
            original = MemoryUnitOfWork.save_entity

            def killed_entity_write(self, entity):
                original(self, entity)
                if entity.entity_type == "od5a_physical_observation":
                    os._exit(96)

            MemoryUnitOfWork.save_entity = killed_entity_write
            apply_od5a_physical_observation(
                store, effect_id=effect_id, applied_at=at,
            )
        else:
            original = MemoryUnitOfWork.save_business_effect

            def killed_certificate_write(self, certificate):
                original(self, certificate)
                os._exit(96)

            MemoryUnitOfWork.save_business_effect = killed_certificate_write
            certify_od5a_physical_observation(
                store, effect_id=effect_id, completed_at=at,
            )


@pytest.mark.parametrize("kill_point", ["apply", "certify"])
def test_real_os_death_inside_uncommitted_transaction_rolls_back_and_recovers(
    tmp_path, kill_point,
):
    path = tmp_path / "uncommitted.db"
    event, effect_id = _stage(path)
    at = event.occurred_at + timedelta(seconds=2)
    if kill_point == "certify":
        with SQLitePersistence(path) as store:
            apply_od5a_physical_observation(
                store, effect_id=effect_id, applied_at=at,
            )
    before = _snapshot(path, "org:alpha", event.event_id, effect_id)
    worker = get_context("spawn").Process(
        target=_die_during_uncommitted_write,
        args=(str(path), effect_id, at, kill_point),
    )
    worker.start()
    worker.join(timeout=30)
    if worker.is_alive():
        worker.kill()
        worker.join()
        pytest.fail("worker did not terminate in uncommitted UoW")
    assert worker.exitcode == 96
    assert _snapshot(path, "org:alpha", event.event_id, effect_id) == before
    with SQLitePersistence(path) as store:
        apply_od5a_physical_observation(store, effect_id=effect_id, applied_at=at)
        cert = certify_od5a_physical_observation(
            store, effect_id=effect_id, completed_at=at,
        )
    assert _snapshot(path, "org:alpha", event.event_id, effect_id)[2] == cert


def _parallel_certificate_worker(path: str, effect_id: str, at, barrier, reports):
    """Separate independent SQLite connection and process: same event-keyed effect."""
    with SQLitePersistence(path) as store:
        barrier.wait(timeout=30)
        observation = apply_od5a_physical_observation(
            store, effect_id=effect_id, applied_at=at,
        )
        certificate = certify_od5a_physical_observation(
            store, effect_id=effect_id, completed_at=at,
        )
        reports.put((observation.version, certificate.receipt_id))


def test_two_os_workers_racing_same_observation_publish_one_certificate(tmp_path):
    path = tmp_path / "concurrent-certification.db"
    event, effect_id = _stage(path)
    at = event.occurred_at + timedelta(seconds=2)
    context = get_context("spawn")
    barrier = context.Barrier(2)
    reports = context.Queue()
    children = [
        context.Process(
            target=_parallel_certificate_worker,
            args=(str(path), effect_id, at, barrier, reports),
        ) for _ in range(2)
    ]
    try:
        for child in children:
            child.start()
        for child in children:
            child.join(timeout=40)
            if child.is_alive():
                child.kill()
                child.join()
                pytest.fail("concurrent OD-5a worker deadlock")
            assert child.exitcode == 0
        outcomes = [reports.get(timeout=5) for _ in children]
        assert outcomes[0] == outcomes[1]
        assert outcomes[0][0] == 1
        observation, command, proof, _ = _snapshot(
            path, "org:alpha", event.event_id, effect_id,
        )
        assert observation.version == 1
        assert command is None
        assert proof.receipt_id == outcomes[0][1]
    finally:
        for child in children:
            if child.is_alive():
                child.kill()
                child.join()
        reports.close()
        reports.join_thread()


def test_observation_must_not_precede_committed_boundary_ack(tmp_path):
    path = tmp_path / "backdated-observation.db"
    event, effect_id = _stage(path)
    with SQLitePersistence(path) as store:
        with pytest.raises(ValueError, match="ACK"):
            apply_od5a_physical_observation(
                store, effect_id=effect_id, applied_at=event.occurred_at,
            )
        assert store.entity("od5a_physical_observation", event.event_id) is None
        assert store.command(effect_id) is not None
        assert store.business_effect(effect_id) is None


def test_earlier_than_observation_forged_prior_certificate_is_rejected(tmp_path):
    from sose.composition.effects import BusinessEffectApplied

    path = tmp_path / "early-forged-certificate.db"
    event, effect_id = _stage(path)
    applied_at = event.occurred_at + timedelta(seconds=2)
    with SQLitePersistence(path) as store:
        observation = apply_od5a_physical_observation(
            store, effect_id=effect_id, applied_at=applied_at,
        )
        with store.transaction() as uow:
            command = uow.get_command(effect_id)
            prior = BusinessEffectApplied.from_applied(
                effect_id=effect_id,
                boundary_message_id=command.causation_id,
                correlation_id=command.correlation_id,
                entity_type=observation.entity_type,
                entity_id=observation.id,
                terminal_state=observation.state,
                entity_version=observation.version,
                # After the ACK at t+1 but BEFORE observed terminal state at t+2.
                completed_at=event.occurred_at + timedelta(seconds=1),
            )
            uow.save_business_effect(prior)
            uow.delete_command(effect_id)
        with pytest.raises(ValueError, match="certificate"):
            apply_od5a_physical_observation(
                store, effect_id=effect_id, applied_at=applied_at,
            )
        with pytest.raises(ValueError, match="certificate"):
            certify_od5a_physical_observation(
                store, effect_id=effect_id,
                completed_at=event.occurred_at + timedelta(seconds=3),
            )


def test_noncanonical_stored_boundary_message_cannot_certify_even_with_valid_ack(
    tmp_path,
):
    from sose.composition.boundary import BoundaryService

    path = tmp_path / "noncanonical-message.db"
    event, effect_id = _stage(path)
    with SQLitePersistence(path) as store:
        with store.transaction() as uow:
            pending = uow.get_command(effect_id)
            canonical = uow.get_boundary_message(pending.causation_id)
        malicious = replace(
            canonical, message_id="forged:" + canonical.message_id,
        )
        forged_delivery = BoundaryService(store).publish(malicious)
        now = event.occurred_at + timedelta(seconds=2)
        lease = BoundaryService(store).claim_next(
            owner_id="malicious-worker", now=now,
            lease_duration=timedelta(seconds=30),
            message_id=malicious.message_id,
        )
        assert lease is not None
        receipt = consume_od5a_physical_completion(
            store, lease=lease, now=now,
        )
        assert receipt.delivery_id == forged_delivery.delivery_id
        with pytest.raises(ValueError, match="canonical"):
            apply_od5a_physical_observation(
                store, effect_id=receipt.consumer_effect_id,
                applied_at=now,
            )
        assert store.business_effect(receipt.consumer_effect_id) is None
        assert store.command(receipt.consumer_effect_id) is not None
        assert store.command(effect_id) is not None


@pytest.mark.parametrize("expiry", ["expired", "missing"])
def test_forged_expired_or_missing_lease_ack_cannot_materialize_effect(
    tmp_path, expiry,
):
    """Certification must revalidate claim fencing, not trust ACK status."""
    path = tmp_path / "invalid-lease-ack.db"
    event, effect_id = _stage(path)
    with SQLitePersistence(path) as store:
        with store.transaction() as uow:
            command = uow.get_command(effect_id)
            delivery = next(
                d for d in uow.boundary_deliveries()
                if d.message_id == command.causation_id
            )
            expiry_at = (
                None if expiry == "missing" else delivery.consumed_at
            )
            uow.save_boundary_delivery(
                replace(delivery, lease_expires_at=expiry_at),
            )
        with pytest.raises(ValueError, match="ACK"):
            apply_od5a_physical_observation(
                store, effect_id=effect_id,
                applied_at=event.occurred_at + timedelta(seconds=2),
            )
        assert store.entity("od5a_physical_observation", event.event_id) is None
        assert store.business_effect(effect_id) is None
        assert store.command(effect_id) is not None


@pytest.mark.parametrize("invalid_claim", [
    "missing_owner", "zero_epoch", "zero_attempts",
    "inconsistent_epoch", "claim_before_event",
])
def test_consumed_ack_without_actual_fenced_claim_cannot_certify(
    tmp_path, invalid_claim,
):
    """Consumed status alone is not a legitimate fenced transport transition."""
    path = tmp_path / "forged-claim.db"
    event, effect_id = _stage(path)
    with SQLitePersistence(path) as store:
        with store.transaction() as uow:
            command = uow.get_command(effect_id)
            delivery = next(
                d for d in uow.boundary_deliveries()
                if d.message_id == command.causation_id
            )
            corrupted = {
                "missing_owner": {"claim_owner_id": None},
                "zero_epoch": {"claim_epoch": 0},
                "zero_attempts": {"attempts": 0},
                "inconsistent_epoch": {"claim_epoch": delivery.attempts + 1},
                "claim_before_event": {
                    "claimed_at": event.occurred_at - timedelta(seconds=1)
                },
            }
            uow.save_boundary_delivery(replace(
                delivery, **corrupted[invalid_claim],
            ))
        with pytest.raises(ValueError, match="ACK"):
            apply_od5a_physical_observation(
                store, effect_id=effect_id,
                applied_at=event.occurred_at + timedelta(seconds=2),
            )
        assert store.entity("od5a_physical_observation", event.event_id) is None
        assert store.business_effect(effect_id) is None
        assert store.command(effect_id) is not None

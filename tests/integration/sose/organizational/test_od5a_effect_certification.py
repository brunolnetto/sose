"""Evidence that physical-event ACK, downstream application and certification differ."""
from __future__ import annotations

from datetime import timedelta

import pytest

from sose.composition.effects import BusinessEffectService
from sose.organizational.model_spec import EvidenceClass, ModelSpec
from sose.organizational.od5a_admission import OD5aChargedCost, compile_od5a_arms
from sose.organizational.od5a_boundary import (
    claim_od5a_physical_completion, consume_od5a_physical_completion,
    publish_od5a_physical_completion,
)
from sose.organizational.od5a_durable import (
    advance_od5a_durable, initialize_od5a_durable, od5a_durable_events,
)
from sose.organizational.od5a_effects import apply_od5a_physical_observation
from sose.organizational.od5a_mechanisms import OD5aWorkItem
from sose.persistence.sqlite import SQLitePersistence


def _baseline():
    model = ModelSpec(
        stations={"a": {}, "b": {}, "c": {}},
        routing={"dependency_edges": [["a", "b"], ["b", "c"]]},
        demand={"arrival_process": "exogenous"},
        parameters={
            "arrival_rate": 0.8, "execution_slots": 1,
            "coordination_latency": 0.1, "service_cv": 0.5,
        },
        parameter_evidence={
            name: EvidenceClass.ASSUMED for name in (
                "arrival_rate", "execution_slots", "coordination_latency", "service_cv",
            )
        },
    )
    costs = {name: OD5aChargedCost(8, 1, 1, 1) for name in (
        "capacity_only", "topology_only", "coordination_only", "structural_combined",
    )}
    return compile_od5a_arms(
        baseline=model, expanded_capacity=2,
        removed_dependency_edges=(("a", "b"),),
        reduced_coordination_latency=0.05, declared_costs=costs,
    )[0]


def _committed_physical_ingress(path, job_id):
    with SQLitePersistence(path) as store:
        initialize_od5a_durable(
            store, job_id=job_id, arm=_baseline(),
            items=(OD5aWorkItem(
                item_id=f"item:{job_id}", arrival_at=0.0,
                service_by_station=(("a", 0.2), ("b", 0.3), ("c", 0.1)),
            ),),
        )
        advance_od5a_durable(store, job_id=job_id)
        advance_od5a_durable(store, job_id=job_id)
        event = next(e for e in od5a_durable_events(store, job_id=job_id)
                     if e.payload["kind"] == "execution")
        publication = publish_od5a_physical_completion(
            store, job_id=job_id, event_id=event.event_id,
        )
        now = event.occurred_at + timedelta(seconds=1)
        lease = claim_od5a_physical_completion(
            store, job_id=job_id, owner_id="worker", now=now,
            lease_duration=timedelta(seconds=10),
        )
        assert lease is not None
        ack = consume_od5a_physical_completion(store, lease=lease, now=now)
        assert ack.message_id == publication.message_id
        return event, publication, ack, now


def test_ack_and_command_are_not_an_applied_or_certified_physical_effect(tmp_path):
    path = tmp_path / "truth.db"
    event, _, ack, now = _committed_physical_ingress(path, "org:alpha")
    with SQLitePersistence(path) as store:
        assert store.command(ack.consumer_effect_id) is not None
        assert store.entity("od5a_physical_observation", event.event_id) is None
        assert store.business_effect(ack.consumer_effect_id) is None
        with pytest.raises(ValueError, match="terminal|certified"):
            BusinessEffectService(store).complete(
                effect_id=ack.consumer_effect_id, completed_at=now,
            )
        assert store.command(ack.consumer_effect_id) is not None

    with SQLitePersistence(path) as restarted:
        applied = apply_od5a_physical_observation(
            restarted, effect_id=ack.consumer_effect_id, applied_at=now,
        )
        assert applied.id == event.event_id
        assert applied.entity_type == "od5a_physical_observation"
        assert applied.state == "applied"
        assert applied.attributes["source_event_id"] == event.event_id
        assert applied.attributes["model_spec_hash"] == event.payload["model_spec_hash"]
        assert applied.attributes["resource"] == event.payload["resource"]
        assert applied.attributes["correlation_id"] == "org:alpha"
        assert restarted.command(ack.consumer_effect_id) is not None
        assert restarted.business_effect(ack.consumer_effect_id) is None

    with SQLitePersistence(path) as certifier:
        cert = BusinessEffectService(certifier).complete(
            effect_id=ack.consumer_effect_id, completed_at=now,
        )
        assert cert.effect_id == ack.consumer_effect_id
        assert cert.entity_type == "od5a_physical_observation"
        assert cert.entity_id == event.event_id
        assert cert.correlation_id == "org:alpha"
        assert cert.entity_version == applied.version
        assert certifier.command(ack.consumer_effect_id) is None
        assert certifier.business_effect(ack.consumer_effect_id) == cert
        assert BusinessEffectService(certifier).complete(
            effect_id=ack.consumer_effect_id, completed_at=now,
        ) == cert


def test_reopened_worker_after_application_cannot_duplicate_observation(tmp_path):
    path = tmp_path / "kill-after-apply.db"
    event, _, ack, now = _committed_physical_ingress(path, "org:alpha")
    with SQLitePersistence(path) as worker:
        first = apply_od5a_physical_observation(
            worker, effect_id=ack.consumer_effect_id, applied_at=now,
        )
        observed = tuple(e for e in worker.events()
                         if e.entity_type == "od5a_physical_observation")
        assert len(observed) == 1

    with SQLitePersistence(path) as recovered:
        second = apply_od5a_physical_observation(
            recovered, effect_id=ack.consumer_effect_id, applied_at=now,
        )
        assert second == first
        assert tuple(e for e in recovered.events()
                     if e.entity_type == "od5a_physical_observation") == observed
        cert = BusinessEffectService(recovered).complete(
            effect_id=ack.consumer_effect_id, completed_at=now,
        )
        assert cert.entity_id == event.event_id


def test_unacknowledged_or_cross_organization_intent_cannot_be_applied(tmp_path):
    path = tmp_path / "cross-organization.db"
    event, _, ack, now = _committed_physical_ingress(path, "org:alpha")
    with SQLitePersistence(path) as store:
        with pytest.raises((KeyError, ValueError), match="Command|command"):
            apply_od5a_physical_observation(
                store, effect_id="unknown-command", applied_at=now,
            )
        from dataclasses import replace

        with store.transaction() as uow:
            command = uow.get_command(ack.consumer_effect_id)
            assert command is not None
            uow.save_command(replace(
                command, command_id="forged-cross-org-command",
                correlation_id="org:wrong",
            ))
        with pytest.raises(ValueError, match="correlation"):
            apply_od5a_physical_observation(
                store, effect_id="forged-cross-org-command", applied_at=now,
            )
        assert store.entity("od5a_physical_observation", event.event_id) is None
        assert store.command(ack.consumer_effect_id) == command


def test_counterfeit_applied_entity_does_not_get_a_certificate(tmp_path):
    """An entity state label alone cannot certify contradictory physical truth."""
    from sose.domain.entity import Entity

    path = tmp_path / "counterfeit.db"
    event, publication, ack, now = _committed_physical_ingress(path, "org:alpha")
    with SQLitePersistence(path) as store:
        with store.transaction() as uow:
            counterfeit = Entity(
                id=event.event_id, entity_type="od5a_physical_observation",
                state="applied", created_at=now, updated_at=now, version=1,
                attributes={
                    "source_event_id": event.event_id,
                    "correlation_id": "org:alpha",
                    "boundary_message_id": publication.message_id,
                    "model_spec_hash": "WRONG",
                    "config_epoch": 0,
                    "resource": event.payload["resource"],
                    "start_us": 0, "end_us": 0,
                },
            )
            uow.save_entity(counterfeit)
        with pytest.raises(ValueError, match="source|model|physical"):
            BusinessEffectService(store).complete(
                effect_id=ack.consumer_effect_id, completed_at=now,
            )
        assert store.business_effect(ack.consumer_effect_id) is None
        assert store.command(ack.consumer_effect_id) is not None


def test_independent_organizations_do_not_alias_applied_event_certificates(tmp_path):
    path = tmp_path / "organizations.db"
    alpha, _, ack_a, at_a = _committed_physical_ingress(path, "org:alpha")
    beta, _, ack_b, at_b = _committed_physical_ingress(path, "org:beta")
    assert alpha.event_id != beta.event_id
    with SQLitePersistence(path) as store:
        applied_b = apply_od5a_physical_observation(
            store, effect_id=ack_b.consumer_effect_id, applied_at=at_b,
        )
        applied_a = apply_od5a_physical_observation(
            store, effect_id=ack_a.consumer_effect_id, applied_at=at_a,
        )
        assert applied_a.id != applied_b.id
        certified = BusinessEffectService(store)
        cb = certified.complete(effect_id=ack_b.consumer_effect_id, completed_at=at_b)
        ca = certified.complete(effect_id=ack_a.consumer_effect_id, completed_at=at_a)
        assert cb.correlation_id == "org:beta"
        assert ca.correlation_id == "org:alpha"
        assert cb.receipt_id != ca.receipt_id
        assert store.business_effects() == tuple(
            sorted((ca, cb), key=lambda e: e.receipt_id)
        ) or set(store.business_effects()) == {ca, cb}

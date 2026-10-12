"""OD-5a production boundary ingress falsifiers (non-confirmatory pilot)."""
from __future__ import annotations

from datetime import timedelta

import pytest

from sose.composition.boundary import (
    BoundaryMessage, BoundaryService, StaleBoundaryClaimError,
)
from sose.organizational.model_spec import EvidenceClass, ModelSpec
from sose.organizational.od5a_admission import OD5aChargedCost, compile_od5a_arms
from sose.organizational.od5a_boundary import (
    claim_od5a_physical_completion,
    consume_od5a_physical_completion,
    publish_od5a_physical_completion,
)
from sose.organizational.od5a_durable import (
    advance_od5a_durable, initialize_od5a_durable, od5a_durable_events,
)
from sose.organizational.od5a_mechanisms import OD5aWorkItem
from sose.persistence.sqlite import SQLitePersistence


def _arms():
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
    return {arm.arm_id: arm for arm in compile_od5a_arms(
        baseline=model,
        expanded_capacity=2,
        removed_dependency_edges=(("a", "b"),),
        reduced_coordination_latency=0.05,
        declared_costs={
            name: OD5aChargedCost(8, 1, 1, 1)
            for name in (
                "capacity_only", "topology_only", "coordination_only",
                "structural_combined",
            )
        },
    )}


def _seed(path, job_id="org:a", arm="baseline"):
    with SQLitePersistence(path) as store:
        initialize_od5a_durable(
            store, job_id=job_id, arm=_arms()[arm],
            items=(OD5aWorkItem(
                item_id=f"item:{job_id}", arrival_at=0.0,
                service_by_station=(("a", 0.2), ("b", 0.3), ("c", 0.1)),
            ),),
        )
        advance_od5a_durable(store, job_id=job_id)  # committed arrival
        advance_od5a_durable(store, job_id=job_id)  # committed physical completion
        events = od5a_durable_events(store, job_id=job_id)
        return next(e for e in events if e.payload["kind"] == "execution")


@pytest.mark.parametrize("arm", [
    "baseline", "capacity_only", "topology_only", "coordination_only",
    "structural_combined",
])
def test_restarted_publication_claim_ack_command_preserves_real_cause(tmp_path, arm):
    path = tmp_path / "od5a-ingress.db"
    event = _seed(path, arm=arm)
    with SQLitePersistence(path) as publisher:
        receipt = publish_od5a_physical_completion(
            publisher, job_id="org:a", event_id=event.event_id,
        )
        assert publish_od5a_physical_completion(
            publisher, job_id="org:a", event_id=event.event_id,
        ) == receipt
        with publisher.transaction() as uow:
            message = uow.get_boundary_message(receipt.message_id)
            assert message.causation_kind == "event"
            assert message.causation_id == event.event_id
            assert message.correlation_id == "org:a"
            assert message.payload()["model_spec_hash"] == event.payload["model_spec_hash"]

    now = event.occurred_at + timedelta(seconds=1)
    with SQLitePersistence(path) as worker:
        lease = claim_od5a_physical_completion(
            worker, job_id="org:a", owner_id="worker-1",
            now=now, lease_duration=timedelta(seconds=2),
        )
        assert lease is not None and lease.message_id == receipt.message_id
        with worker.transaction() as uow:
            assert uow.get_command(lease.message_id) is None

    with SQLitePersistence(path) as recovered:
        ack = consume_od5a_physical_completion(
            recovered, lease=lease, now=now + timedelta(seconds=1),
        )
        assert ack.delivery_id == receipt.delivery_id
        with recovered.transaction() as uow:
            command = uow.get_command(ack.consumer_effect_id)
            assert command is not None
            assert command.name == "od5a.stage_physical_completion.v1"
            assert command.entity_type == "od5a_physical_observation"
            assert command.entity_id == event.event_id
            assert command.correlation_id == "org:a"
            assert command.causation_id == receipt.message_id
            assert command.payload["event_id"] == event.event_id
            assert uow.get_business_effect(ack.consumer_effect_id) is None
        assert consume_od5a_physical_completion(
            recovered, lease=lease, now=now + timedelta(seconds=1),
        ) == ack
        assert claim_od5a_physical_completion(
            recovered, job_id="org:a", owner_id="worker-2",
            now=now + timedelta(seconds=3), lease_duration=timedelta(seconds=1),
        ) is None


def test_expired_claim_fencing_survives_process_reopen_and_does_not_duplicate_command(tmp_path):
    path = tmp_path / "fencing.db"
    event = _seed(path)
    with SQLitePersistence(path) as store:
        receipt = publish_od5a_physical_completion(
            store, job_id="org:a", event_id=event.event_id,
        )
        start = event.occurred_at + timedelta(seconds=1)
        stale = claim_od5a_physical_completion(
            store, job_id="org:a", owner_id="worker-old", now=start,
            lease_duration=timedelta(seconds=1),
        )
        assert stale is not None
    with SQLitePersistence(path) as store:
        now = start + timedelta(seconds=2)
        fresh = claim_od5a_physical_completion(
            store, job_id="org:a", owner_id="worker-new", now=now,
            lease_duration=timedelta(seconds=10),
        )
        assert fresh is not None and fresh.epoch == stale.epoch + 1
        with pytest.raises(StaleBoundaryClaimError):
            consume_od5a_physical_completion(store, lease=stale, now=now)
        with store.transaction() as uow:
            assert uow.get_boundary_consumption(receipt.delivery_id) is None
        result = consume_od5a_physical_completion(store, lease=fresh, now=now)
        assert result.delivery_id == receipt.delivery_id
        with store.transaction() as uow:
            assert uow.get_command(result.consumer_effect_id)
            assert uow.get_business_effect(result.consumer_effect_id) is None


def test_physical_boundary_rejects_arrival_foreign_event_and_nonexistent_event(tmp_path):
    path = tmp_path / "invalid.db"
    _seed(path, job_id="org:a")
    _seed(path, job_id="org:b")
    with SQLitePersistence(path) as store:
        events = od5a_durable_events(store, job_id="org:a")
        arrival = next(e for e in events if e.payload["kind"] == "arrival")
        physical = next(e for e in events if e.payload["kind"] == "execution")
        for identity in (arrival.event_id, "nonexistent-id"):
            with pytest.raises(ValueError):
                publish_od5a_physical_completion(
                    store, job_id="org:a", event_id=identity,
                )
        with pytest.raises(ValueError):
            publish_od5a_physical_completion(
                store, job_id="org:b", event_id=physical.event_id,
            )
        with store.transaction() as uow:
            assert uow.boundary_deliveries() == ()


def test_organization_claim_filter_is_local_even_with_shared_sqlite_database(tmp_path):
    path = tmp_path / "multi-org.db"
    a, b = _seed(path, job_id="org:a"), _seed(path, job_id="org:b")
    with SQLitePersistence(path) as store:
        pub_a = publish_od5a_physical_completion(
            store, job_id="org:a", event_id=a.event_id,
        )
        pub_b = publish_od5a_physical_completion(
            store, job_id="org:b", event_id=b.event_id,
        )
        now = max(a.occurred_at, b.occurred_at) + timedelta(seconds=1)
        first = claim_od5a_physical_completion(
            store, job_id="org:b", owner_id="b-worker", now=now,
            lease_duration=timedelta(seconds=30),
        )
        second = claim_od5a_physical_completion(
            store, job_id="org:a", owner_id="a-worker", now=now,
            lease_duration=timedelta(seconds=30),
        )
        assert first.message_id == pub_b.message_id
        assert second.message_id == pub_a.message_id
        assert first.epoch == second.epoch == 1
        assert consume_od5a_physical_completion(
            store, lease=first, now=now,
        ).message_id == pub_b.message_id
        assert consume_od5a_physical_completion(
            store, lease=second, now=now,
        ).message_id == pub_a.message_id


def test_forged_boundary_payload_cannot_ack_into_command(tmp_path):
    path = tmp_path / "forged.db"
    event = _seed(path)
    with SQLitePersistence(path) as store:
        receipt = publish_od5a_physical_completion(
            store, job_id="org:a", event_id=event.event_id,
        )
        with store.transaction() as uow:
            true = uow.get_boundary_message(receipt.message_id)
        hostile = BoundaryMessage.create(
            contract_name=true.contract_name, contract_version=true.contract_version,
            source_domain=true.source_domain, source_identity=true.source_identity,
            destination_domain=true.destination_domain,
            occurrence_key="forged", correlation_id=true.correlation_id,
            causation_id=true.causation_id, causation_kind="event",
            produced_at=true.produced_at,
            payload={**true.payload(), "model_spec_hash": "forged"},
        )
        forged = BoundaryService(store).publish(hostile)
        now = event.occurred_at + timedelta(seconds=1)
        lease = BoundaryService(store).claim_next(
            owner_id="worker", now=now, lease_duration=timedelta(seconds=30),
            message_id=hostile.message_id,
        )
        assert lease is not None and lease.delivery_id == forged.delivery_id
        with pytest.raises(ValueError, match="diverges"):
            consume_od5a_physical_completion(store, lease=lease, now=now)
        with store.transaction() as uow:
            assert uow.get_boundary_consumption(forged.delivery_id) is None


@pytest.mark.parametrize("forged_kind,forged_resource", [
    ("execution", "coordination:0"),
    ("coordination", "execution:0"),
])
def test_cross_class_physical_resource_event_never_becomes_a_boundary_message(
    tmp_path, forged_kind, forged_resource,
):
    """A physically impossible event must never acquire a delivery/ACK."""
    from dataclasses import replace

    path = tmp_path / "cross-kind.db"
    reference = _seed(path)
    forged = replace(
        reference,
        event_id=f"{reference.event_id}-wrong-{forged_kind}",
        name=f"od5a.{forged_kind}.v1",
        payload={
            **reference.payload, "kind": forged_kind, "resource": forged_resource,
        },
    )
    with SQLitePersistence(path) as store:
        with store.transaction() as uow:
            uow.append_event(forged)
        with pytest.raises(ValueError, match="physical resource class"):
            publish_od5a_physical_completion(
                store, job_id="org:a", event_id=forged.event_id,
            )
        with store.transaction() as uow:
            assert uow.boundary_deliveries() == ()


@pytest.mark.parametrize("forgery", [
    "invented_event_id",
    "canonical_but_unfinished_station",
    "fabricated_item",
])
def test_same_class_forged_event_without_authoritative_stage_trail_is_rejected(
    tmp_path, forgery,
):
    """Persisting a plausible DomainEvent alone cannot invent physical work."""
    from dataclasses import replace
    from sose.core.identity import deterministic_id

    path = tmp_path / "unearned-physical.db"
    authentic = _seed(path)
    if forgery == "invented_event_id":
        fake = replace(authentic, event_id=authentic.event_id + ":forged")
    else:
        item = (authentic.payload["item_id"] if forgery == "canonical_but_unfinished_station"
                else "unadmitted-item")
        station = "b" if forgery == "canonical_but_unfinished_station" else "a"
        fake = replace(
            authentic,
            event_id=deterministic_id(
                "od5a-durable-event", "org:a", "execution", item, station, station,
            ),
            payload={
                **authentic.payload, "item_id": item,
                "source": station, "target": station,
            },
        )
    with SQLitePersistence(path) as store:
        with store.transaction() as uow:
            uow.append_event(fake)
        with pytest.raises(ValueError, match="identity|trail|unadmitted"):
            publish_od5a_physical_completion(
                store, job_id="org:a", event_id=fake.event_id,
            )
        with store.transaction() as uow:
            assert uow.boundary_deliveries() == ()


def test_committed_coordination_event_has_authoritative_received_trail(tmp_path):
    path = tmp_path / "real-coordination.db"
    _seed(path)
    with SQLitePersistence(path) as store:
        for _ in range(20):
            if any(e.payload["kind"] == "coordination"
                   for e in od5a_durable_events(store, job_id="org:a")):
                break
            assert advance_od5a_durable(store, job_id="org:a")
        coordination = next(
            e for e in od5a_durable_events(store, job_id="org:a")
            if e.payload["kind"] == "coordination"
        )
        receipt = publish_od5a_physical_completion(
            store, job_id="org:a", event_id=coordination.event_id,
        )
        lease = claim_od5a_physical_completion(
            store, job_id="org:a", owner_id="coord-worker",
            now=coordination.occurred_at + timedelta(seconds=1),
            lease_duration=timedelta(seconds=30),
        )
        assert lease is not None
        result = consume_od5a_physical_completion(
            store, lease=lease, now=coordination.occurred_at + timedelta(seconds=1),
        )
        assert result.message_id == receipt.message_id

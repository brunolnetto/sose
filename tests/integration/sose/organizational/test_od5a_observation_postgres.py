"""OD-5a downstream certification cross-adapter conformance (actual PostgreSQL).

Only runs when CI or a developer supplies SOSE_TEST_POSTGRES_DSN.
A generic PostgreSQL smoke gate alone does not establish OD-5a semantics.
"""
from __future__ import annotations

from datetime import timedelta
import os
from uuid import uuid4

import pytest

from sose.organizational.model_spec import ModelSpec, EvidenceClass
from sose.organizational.od5a_admission import OD5aChargedCost, compile_od5a_arms
from sose.organizational.od5a_mechanisms import OD5aWorkItem
from sose.organizational.od5a_durable import (
    initialize_od5a_durable, advance_od5a_durable, od5a_durable_events,
)
from sose.organizational.od5a_boundary import (
    publish_od5a_physical_completion,
    claim_od5a_physical_completion,
    consume_od5a_physical_completion,
)
from sose.organizational.od5a_observation import (
    apply_od5a_physical_observation, certify_od5a_physical_observation,
)
from sose.persistence.sqlite import SQLitePersistence


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
            k: EvidenceClass.ASSUMED for k in (
                "arrival_rate", "execution_slots", "coordination_latency", "service_cv",
            )
        },
    )
    arms = compile_od5a_arms(
        baseline=baseline, expanded_capacity=2,
        removed_dependency_edges=(("a", "b"),),
        reduced_coordination_latency=0.05,
        declared_costs={
            k: OD5aChargedCost(8, 1, 1, 1)
            for k in ("capacity_only", "topology_only",
                      "coordination_only", "structural_combined")
        },
    )
    return arms[0]


def _sequence(factory):
    job_id = "postgres:od5a:independent"
    with factory() as store:
        initialize_od5a_durable(
            store, job_id=job_id, arm=_arm(),
            items=(OD5aWorkItem(
                "item:1", 0.0, (("a", 0.2), ("b", 0.3), ("c", 0.1)),
            ),),
        )
        assert advance_od5a_durable(store, job_id=job_id)
        assert advance_od5a_durable(store, job_id=job_id)
        event = next(
            e for e in od5a_durable_events(store, job_id=job_id)
            if e.payload["kind"] == "execution"
        )
    with factory() as store:
        published = publish_od5a_physical_completion(
            store, job_id=job_id, event_id=event.event_id,
        )
    at = event.occurred_at + timedelta(seconds=2)
    with factory() as store:
        lease = claim_od5a_physical_completion(
            store, job_id=job_id, owner_id="postgres-worker",
            now=at, lease_duration=timedelta(seconds=30),
        )
        assert lease is not None
    with factory() as store:
        receipt = consume_od5a_physical_completion(
            store, lease=lease, now=at,
        )
        assert receipt.message_id == published.message_id
        assert store.business_effect(receipt.consumer_effect_id) is None
    with factory() as store:
        observation = apply_od5a_physical_observation(
            store, effect_id=receipt.consumer_effect_id, applied_at=at,
        )
        assert store.business_effect(receipt.consumer_effect_id) is None
        assert store.command(receipt.consumer_effect_id) is not None
    with factory() as store:
        certificate = certify_od5a_physical_observation(
            store, effect_id=receipt.consumer_effect_id, completed_at=at,
        )
        assert store.command(receipt.consumer_effect_id) is None
    with factory() as store:
        assert apply_od5a_physical_observation(
            store, effect_id=receipt.consumer_effect_id, applied_at=at,
        ) == observation
        assert certify_od5a_physical_observation(
            store, effect_id=receipt.consumer_effect_id,
            completed_at=at + timedelta(seconds=1),
        ) == certificate
        assert store.business_effect(receipt.consumer_effect_id) == certificate
        assert store.command(receipt.consumer_effect_id) is None
        with store.transaction() as uow:
            message = uow.get_boundary_message(published.message_id)
            assert message.causation_kind == "event"
            assert message.causation_id == event.event_id
            assert message.correlation_id == job_id
            assert uow.get_boundary_consumption(receipt.delivery_id) == receipt
            deliveries = uow.boundary_deliveries()
            assert len(deliveries) == 1
            # Compare the *entire relevant durable truth*, not only returned
            # receipts. This catches adapter divergence in claim fencing,
            # canonical causation, physical grants and residual Commands.
            snapshot = {
                "event_history": tuple(store.events()),
                "source_entity": uow.get_entity("od5a_durable_pilot", job_id),
                "resource_definitions": tuple(store.resource_definitions()),
                "resource_reservations": tuple(store.resource_reservations()),
                "boundary_messages": tuple(
                    uow.get_boundary_message(d.message_id) for d in deliveries
                ),
                "boundary_deliveries": deliveries,
                "boundary_consumptions": tuple(
                    uow.get_boundary_consumption(d.delivery_id) for d in deliveries
                ),
                "source_physical_event": event,
                "observation": uow.get_entity("od5a_physical_observation", event.event_id),
                "pending_command": uow.get_command(receipt.consumer_effect_id),
                "certificates": uow.business_effects(),
            }
            assert snapshot["observation"] == observation
            assert snapshot["certificates"] == (certificate,)
            assert snapshot["pending_command"] is None
    return snapshot


def test_postgres_exact_domain_and_certificate_truth_matches_sqlite_after_restarts(
    tmp_path,
):
    dsn = os.environ.get("SOSE_TEST_POSTGRES_DSN")
    if not dsn:
        pytest.skip("real PostgreSQL required (SOSE_TEST_POSTGRES_DSN)")
    from sose.persistence.postgres import PostgresPersistence

    namespace = "od5aobs_" + uuid4().hex[:16]
    reference = _sequence(lambda: SQLitePersistence(tmp_path / "control.db"))
    postgres = _sequence(lambda: PostgresPersistence(dsn, namespace=namespace))
    assert postgres == reference

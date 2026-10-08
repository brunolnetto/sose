from __future__ import annotations

from dataclasses import asdict
from pathlib import Path

from sose.examples.logistics.config import LogisticsConfig
from sose.examples.logistics.definition import definition
from sose.examples.logistics.observability import logistics_kpis, logistics_projection
from sose.examples.logistics.process_audit import process_manifest
from sose.backends.simpy import SimPyBackend
from sose.examples.logistics.simulation import (
    ORIGIN,
    PICKUP_DUE,
    build_runtime,
    reconcile_pickup,
    run_failed_retry_path,
    run_happy_path,
    seed_reference,
)
from sose.examples.process_manifest import ProcessEvidence, ProcessMaturity
from sose.persistence.memory import MemoryPersistence


def test_logistics_projection_is_read_only_and_idempotent() -> None:
    persistence, entities = run_happy_path()
    shipment = persistence.entity("shipment", entities.shipment_id)
    assert shipment is not None
    before_version = shipment.version

    left = logistics_projection(persistence, entities=entities)
    right = logistics_projection(persistence, entities=entities)

    assert left == right
    assert left.shipment_id == entities.shipment_id
    assert left.shipment_state == "delivered"
    assert left.service_level == "standard"
    assert left.route == "origin-a:destination-b"
    assert left.delivered is True
    assert left.terminal_outcome == "delivered"
    assert left.shipment_lead_time_seconds is not None
    assert left.shipment_lead_time_seconds >= 0.0

    after = persistence.entity("shipment", entities.shipment_id)
    assert after is not None and after.version == before_version


def test_logistics_kpis_are_derived_from_durable_events() -> None:
    persistence, entities = run_happy_path()

    projection = logistics_projection(persistence, entities=entities)
    kpis = logistics_kpis(persistence, entities=entities)

    assert kpis.shipment_lead_time_seconds == projection.shipment_lead_time_seconds
    assert kpis.transition_count > 0
    assert kpis.delay_count == 0
    assert kpis.delivery_attempt_count == 1
    assert kpis.failed_attempt_count == 0
    assert kpis.delivered_attempt_count == 1
    assert kpis.delivered is True
    assert set(asdict(kpis)) == {
        "shipment_lead_time_seconds",
        "transition_count",
        "delay_count",
        "delivery_attempt_count",
        "failed_attempt_count",
        "delivered_attempt_count",
        "delivered",
    }


def test_logistics_projection_does_not_invent_terminal_outcome_before_execution() -> None:
    persistence = MemoryPersistence()
    entities = seed_reference(persistence)

    projection = logistics_projection(persistence, entities=entities)
    kpis = logistics_kpis(persistence, entities=entities)

    assert projection.shipment_state == "created"
    assert projection.delivered is False
    assert projection.terminal_outcome is None
    assert projection.shipment_lead_time_seconds is None
    assert kpis.transition_count == 0
    assert kpis.delivery_attempt_count == 0
    assert kpis.delivered is False


def test_logistics_kpis_preserve_failed_attempt_history_after_retry_success() -> None:
    persistence, entities = run_failed_retry_path()

    projection = logistics_projection(persistence, entities=entities)
    kpis = logistics_kpis(persistence, entities=entities)

    assert projection.shipment_state == "delivered"
    assert projection.delivered is True
    assert kpis.delivery_attempt_count == 2
    assert kpis.failed_attempt_count == 1
    assert kpis.delivered_attempt_count == 1
    assert kpis.delay_count == 1



def test_logistics_kpis_include_uncorrelated_terminal_exception() -> None:
    persistence = MemoryPersistence()
    entities = seed_reference(persistence)
    context, engine = build_runtime(persistence)
    backend = SimPyBackend(origin=ORIGIN)
    engine.rebuild_backend(backend)

    backend.run_until(PICKUP_DUE)
    assert reconcile_pickup(persistence, engine, backend, entities=entities)

    shipment = persistence.entity("shipment", entities.shipment_id)
    assert shipment is not None and shipment.state == "picked_up"
    command = context.commands.create(
        "mark_lost",
        target=shipment,
        key=("logistics-manual-loss", shipment.id),
    )
    engine.dispatch(command)

    projection = logistics_projection(persistence, entities=entities)
    kpis = logistics_kpis(persistence, entities=entities)
    assert projection.terminal_outcome == "lost"
    assert projection.delivered is False
    assert projection.shipment_lead_time_seconds is not None
    assert kpis.transition_count >= 3


def test_logistics_pc5_observability_evidence_is_complete() -> None:
    manifest = process_manifest()

    assert manifest.maturity is ProcessMaturity.PC5_OBSERVABLE
    assert manifest.is_complete_process_canonical
    assert not manifest.is_integrated_process_canonical
    assert manifest.missing_for(ProcessMaturity.PC5_OBSERVABLE) == frozenset()
    assert manifest.kpis == frozenset(
        {
            "shipment_lead_time_seconds",
            "transition_count",
            "delay_count",
            "delivery_attempt_count",
            "failed_attempt_count",
            "delivered_attempt_count",
            "delivered",
        }
    )
    for evidence in (
        ProcessEvidence.KPIS,
        ProcessEvidence.ERD,
        ProcessEvidence.STATECHART_DOCUMENTATION,
        ProcessEvidence.PROCESS_DIAGRAM,
        ProcessEvidence.PROJECTION_CONTRACT,
        ProcessEvidence.CONFIGURATION_DOCUMENTATION,
    ):
        assert evidence in manifest.evidence
        assert manifest.evidence_sources[evidence]


def test_logistics_normative_pc5_documentation_covers_process_and_config() -> None:
    specification = Path("docs/examples/logistics/specification.md").read_text(
        encoding="utf-8"
    )

    assert "flowchart TD" in specification

    for field_name in LogisticsConfig.model_fields:
        assert f"`{field_name}`" in specification

    for field_name in definition.runtime_mutable_fields:
        assert f"`{field_name}`" in specification

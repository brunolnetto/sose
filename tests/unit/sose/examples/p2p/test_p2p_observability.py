from __future__ import annotations

from dataclasses import asdict
from pathlib import Path

import pytest

from sose.examples.p2p.config import P2PConfig
from sose.examples.p2p.definition import definition
from sose.examples.p2p.observability import p2p_kpis, p2p_projection
from sose.examples.p2p.process_audit import process_manifest
from sose.examples.p2p.simulation import (
    build_runtime,
    run_happy_path,
    run_shortage_backorder,
    seed_happy_path,
)
from sose.examples.process_manifest import ProcessEvidence, ProcessMaturity
from sose.persistence.memory import MemoryPersistence


def _entity_versions(persistence, entities) -> tuple[int, ...]:
    records = (
        persistence.entity("requisition", entities.requisition_id),
        persistence.entity("purchase_order", entities.purchase_order_id),
        persistence.entity("receipt", entities.receipt_id),
        persistence.entity("material_demand", entities.material_demand_id),
    )
    assert all(record is not None for record in records)
    return tuple(record.version for record in records if record is not None)


def test_p2p_projection_is_read_only_idempotent_and_uses_durable_truth() -> None:
    persistence, entities = run_happy_path(quantity=5.0)
    before = _entity_versions(persistence, entities)

    left = p2p_projection(persistence, entities=entities)
    right = p2p_projection(persistence, entities=entities)

    assert left == right
    assert left.requisition_state == "ordered"
    assert left.purchase_order_state == "closed"
    assert left.receipt_state == "stocked"
    assert left.material_demand_state == "consumed"
    assert left.sku == "bearing-6204"
    assert left.quantity == pytest.approx(5.0)
    assert left.supplier == "supplier-a"
    assert left.inventory_level == pytest.approx(0.0)
    assert left.stocked is True
    assert left.consumed is True
    assert left.backordered is False
    assert left.procure_to_consumption_seconds is not None
    assert left.procure_to_consumption_seconds >= 0.0

    assert _entity_versions(persistence, entities) == before


def test_p2p_kpis_are_derived_from_durable_events_and_state() -> None:
    persistence, entities = run_happy_path(quantity=7.0)

    projection = p2p_projection(persistence, entities=entities)
    kpis = p2p_kpis(persistence, entities=entities)

    assert kpis.procure_to_consumption_seconds == projection.procure_to_consumption_seconds
    assert kpis.quantity == pytest.approx(7.0)
    assert kpis.transition_count > 0
    assert kpis.supplier_delay_count == 0
    assert kpis.partial_receipt_count == 0
    assert kpis.rejected_receipt_count == 0
    assert kpis.backorder_count == 0
    assert kpis.consumed is True
    assert set(asdict(kpis)) == {
        "procure_to_consumption_seconds",
        "quantity",
        "transition_count",
        "supplier_delay_count",
        "partial_receipt_count",
        "rejected_receipt_count",
        "backorder_count",
        "consumed",
    }


def test_p2p_projection_does_not_invent_terminal_metrics_before_execution() -> None:
    persistence = MemoryPersistence()
    entities = seed_happy_path(persistence, quantity=3.0)

    projection = p2p_projection(persistence, entities=entities)
    kpis = p2p_kpis(persistence, entities=entities)

    assert projection.requisition_state == "requested"
    assert projection.purchase_order_state == "created"
    assert projection.receipt_state == "pending"
    assert projection.material_demand_state == "open"
    assert projection.stocked is False
    assert projection.consumed is False
    assert projection.backordered is False
    assert projection.procure_to_consumption_seconds is None
    assert kpis.procure_to_consumption_seconds is None
    assert kpis.transition_count == 0
    assert kpis.consumed is False


def test_p2p_kpis_preserve_shortage_backorder_history_after_recovery() -> None:
    persistence, entities = run_shortage_backorder(quantity=5.0)

    projection = p2p_projection(persistence, entities=entities)
    kpis = p2p_kpis(persistence, entities=entities)

    assert projection.material_demand_state == "consumed"
    assert projection.consumed is True
    assert projection.backordered is False
    assert kpis.backorder_count == 1
    assert kpis.consumed is True



def test_p2p_kpis_include_uncorrelated_supplier_delay_transition() -> None:
    persistence = MemoryPersistence()
    entities = seed_happy_path(persistence)
    context, engine = build_runtime(persistence)

    engine.advance_tick()
    engine.advance_tick()
    engine.advance_tick()

    purchase_order = persistence.entity("purchase_order", entities.purchase_order_id)
    assert purchase_order is not None and purchase_order.state == "confirmed"

    before = p2p_kpis(persistence, entities=entities)
    command = context.commands.create(
        "mark_delayed",
        target=purchase_order,
        key=("p2p-supplier-delay", purchase_order.id, "mark-delayed"),
    )
    engine.dispatch(command)

    kpis = p2p_kpis(persistence, entities=entities)
    assert kpis.supplier_delay_count == 1
    assert kpis.transition_count == before.transition_count + 1


def test_p2p_pc5_observability_evidence_is_complete() -> None:
    manifest = process_manifest()

    assert manifest.maturity is ProcessMaturity.PC5_OBSERVABLE
    assert manifest.is_complete_process_canonical
    assert not manifest.is_integrated_process_canonical
    assert manifest.missing_for(ProcessMaturity.PC5_OBSERVABLE) == frozenset()
    assert manifest.kpis == frozenset(
        {
            "procure_to_consumption_seconds",
            "quantity",
            "transition_count",
            "supplier_delay_count",
            "partial_receipt_count",
            "rejected_receipt_count",
            "backorder_count",
            "consumed",
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


def test_p2p_normative_pc5_documentation_is_explicit_and_complete() -> None:
    specification = Path("docs/examples/procure-to-pay/specification.md").read_text(
        encoding="utf-8"
    )

    assert "```mermaid\nerDiagram" in specification
    assert specification.count("stateDiagram-v2") >= 4
    assert "```mermaid\nflowchart TD" in specification

    for field_name in P2PConfig.model_fields:
        assert f"`{field_name}`" in specification

    for field_name in definition.runtime_mutable_fields:
        assert f"`{field_name}`" in specification

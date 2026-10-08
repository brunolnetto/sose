from __future__ import annotations

from dataclasses import asdict
from pathlib import Path

import pytest

from sose.backends.simpy import SimPyBackend
from sose.examples.order_to_cash.config import OrderToCashConfig
from sose.examples.order_to_cash.definition import definition
from sose.examples.order_to_cash.observability import (
    order_to_cash_kpis,
    order_to_cash_projection,
)
from sose.examples.order_to_cash.process_audit import process_manifest
from sose.examples.order_to_cash.simulation import (
    ORIGIN,
    build_runtime,
    collect_receivable,
    ensure_collection_case,
    reconcile_collection,
    reconcile_credit,
    reconcile_fulfillment,
    schedule_due,
    schedule_overdue,
    seed_reference,
    ship_invoice_and_ensure_receivable,
)
from sose.examples.process_manifest import ProcessEvidence, ProcessMaturity
from sose.persistence.memory import MemoryPersistence


def _prepare_collected(*, amount: float = 250.0):
    persistence = MemoryPersistence()
    entities = seed_reference(persistence, amount=amount)
    _, engine = build_runtime(persistence)
    backend = SimPyBackend(origin=ORIGIN)
    engine.rebuild_backend(backend)

    assert reconcile_credit(persistence, engine, entities=entities)
    assert reconcile_fulfillment(
        persistence,
        engine,
        backend,
        entities=entities,
    )
    receivable = ship_invoice_and_ensure_receivable(
        persistence,
        engine,
        entities=entities,
    )
    due_at = schedule_due(
        persistence,
        engine,
        backend,
        entities=entities,
    )
    backend.run_until(due_at)
    assert collect_receivable(persistence, engine, entities=entities)
    return persistence, entities, receivable


def test_o2c_projection_is_read_only_and_idempotent() -> None:
    persistence, entities, receivable = _prepare_collected(amount=300.0)
    order = persistence.entity("sales_order", entities.order_id)
    receivable_before = persistence.entity("receivable", receivable.id)
    assert order is not None and receivable_before is not None
    before_versions = (order.version, receivable_before.version)

    left = order_to_cash_projection(persistence, entities=entities)
    right = order_to_cash_projection(persistence, entities=entities)

    assert left == right
    assert left.order_id == entities.order_id
    assert left.receivable_id == receivable.id
    assert left.collection_case_id is None
    assert left.order_state == "invoiced"
    assert left.receivable_state == "collected"
    assert left.collection_case_state is None
    assert left.amount == pytest.approx(300.0)
    assert left.currency == "USD"
    assert left.collected is True
    assert left.overdue is False
    assert left.collection_open is False
    assert left.order_to_cash_seconds is not None
    assert left.order_to_cash_seconds >= 0.0

    order_after = persistence.entity("sales_order", entities.order_id)
    receivable_after = persistence.entity("receivable", receivable.id)
    assert order_after is not None and receivable_after is not None
    assert (order_after.version, receivable_after.version) == before_versions


def test_o2c_kpis_are_derived_from_durable_state_and_events() -> None:
    persistence, entities, _ = _prepare_collected(amount=425.0)
    projection = order_to_cash_projection(persistence, entities=entities)
    kpis = order_to_cash_kpis(persistence, entities=entities)

    assert kpis.cash_collection is True
    assert kpis.order_to_cash_seconds == projection.order_to_cash_seconds
    assert kpis.amount == pytest.approx(425.0)
    assert kpis.transition_count > 0
    assert kpis.credit_hold_count == 0
    assert kpis.partial_fulfillment_count == 0
    assert kpis.overdue_count == 0
    assert kpis.collection_case_count == 0
    assert kpis.collection_escalation_count == 0
    assert set(asdict(kpis)) == {
        "cash_collection",
        "order_to_cash_seconds",
        "amount",
        "transition_count",
        "credit_hold_count",
        "partial_fulfillment_count",
        "overdue_count",
        "collection_case_count",
        "collection_escalation_count",
    }


def test_o2c_projection_does_not_invent_financial_or_terminal_metrics() -> None:
    persistence = MemoryPersistence()
    entities = seed_reference(persistence, amount=125.0)

    projection = order_to_cash_projection(persistence, entities=entities)
    kpis = order_to_cash_kpis(persistence, entities=entities)

    assert projection.order_state == "submitted"
    assert projection.receivable_id is None
    assert projection.receivable_state is None
    assert projection.collection_case_id is None
    assert projection.collected is False
    assert projection.overdue is False
    assert projection.collection_open is False
    assert projection.order_to_cash_seconds is None
    assert kpis.cash_collection is False
    assert kpis.order_to_cash_seconds is None
    assert kpis.collection_case_count == 0


def test_o2c_overdue_collection_projection_preserves_history() -> None:
    persistence = MemoryPersistence()
    entities = seed_reference(persistence)
    _, engine = build_runtime(persistence)
    backend = SimPyBackend(origin=ORIGIN)
    engine.rebuild_backend(backend)

    assert reconcile_credit(persistence, engine, entities=entities)
    assert reconcile_fulfillment(persistence, engine, backend, entities=entities)
    receivable = ship_invoice_and_ensure_receivable(
        persistence,
        engine,
        entities=entities,
    )
    due_at = schedule_due(persistence, engine, backend, entities=entities)
    backend.run_until(due_at)
    overdue_at = schedule_overdue(persistence, engine, backend, entities=entities)
    backend.run_until(overdue_at)
    case = ensure_collection_case(persistence, engine, entities=entities)
    assert reconcile_collection(
        persistence,
        engine,
        backend,
        entities=entities,
        promise=True,
    )
    followup_at = persistence.scheduled_work()[0].due_at
    backend.run_until(followup_at)

    projection = order_to_cash_projection(persistence, entities=entities)
    kpis = order_to_cash_kpis(persistence, entities=entities)

    assert projection.receivable_id == receivable.id
    assert projection.receivable_state == "overdue"
    assert projection.collection_case_id == case.id
    assert projection.collection_case_state == "escalated"
    assert projection.overdue is True
    assert projection.collection_open is True
    assert projection.order_to_cash_seconds is None
    assert kpis.overdue_count == 1
    assert kpis.collection_case_count == 1
    assert kpis.collection_escalation_count == 1


def test_o2c_pc5_observability_evidence_is_complete() -> None:
    manifest = process_manifest()

    assert manifest.maturity is ProcessMaturity.PC5_OBSERVABLE
    assert manifest.missing_for(ProcessMaturity.PC5_OBSERVABLE) == frozenset()
    assert manifest.kpis == frozenset(
        {
            "cash_collection",
            "order_to_cash_seconds",
            "amount",
            "transition_count",
            "credit_hold_count",
            "partial_fulfillment_count",
            "overdue_count",
            "collection_case_count",
            "collection_escalation_count",
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


def test_o2c_normative_pc5_documentation_is_explicit_and_complete() -> None:
    specification = Path("docs/examples/order-to-cash/specification.md").read_text(
        encoding="utf-8"
    )

    assert "```mermaid\nerDiagram" in specification
    assert specification.count("stateDiagram-v2") >= 3
    assert "```mermaid\nflowchart TD" in specification

    for field_name in OrderToCashConfig.model_fields:
        assert f"`{field_name}`" in specification

    for field_name in definition.runtime_mutable_fields:
        assert f"`{field_name}`" in specification

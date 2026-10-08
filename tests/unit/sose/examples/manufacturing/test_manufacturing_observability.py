from __future__ import annotations

from dataclasses import asdict

import pytest

from sose.backends.simpy import SimPyBackend
from sose.examples.manufacturing.observability import (
    manufacturing_kpis,
    manufacturing_projection,
)
from sose.examples.manufacturing.process_audit import process_manifest
from sose.examples.manufacturing.simulation import (
    ORIGIN,
    build_runtime,
    reconcile_material_issue,
    reconcile_quality_hold,
    reconcile_setup_resources,
    reconcile_wip_output,
    run_happy_path,
    seed_happy_path,
    seed_material,
)
from sose.examples.process_manifest import ProcessEvidence, ProcessMaturity
from sose.persistence.memory import MemoryPersistence


def test_manufacturing_projection_is_read_only_and_idempotent() -> None:
    persistence, entities = run_happy_path(quantity=10.0)
    order = persistence.entity("production_order", entities.production_order_id)
    operation = persistence.entity("manufacturing_operation", entities.operation_id)
    assert order is not None and operation is not None
    before_versions = (order.version, operation.version)

    left = manufacturing_projection(persistence, entities=entities)
    right = manufacturing_projection(persistence, entities=entities)

    assert left == right
    assert left.production_order_id == entities.production_order_id
    assert left.operation_id == entities.operation_id
    assert left.order_state == "completed"
    assert left.operation_state == "done"
    assert left.completed is True
    assert left.planned_quantity == pytest.approx(10.0)
    assert left.raw_material_quantity == pytest.approx(0.0)
    assert left.finished_goods_quantity == pytest.approx(10.0)
    assert left.wip_item_count == 0
    assert left.lead_time_seconds is not None
    assert left.lead_time_seconds >= 0.0

    order_after = persistence.entity("production_order", entities.production_order_id)
    operation_after = persistence.entity("manufacturing_operation", entities.operation_id)
    assert order_after is not None and operation_after is not None
    assert (order_after.version, operation_after.version) == before_versions


def test_manufacturing_kpis_are_derived_from_projection_and_events() -> None:
    persistence, entities = run_happy_path(quantity=8.0)

    projection = manufacturing_projection(persistence, entities=entities)
    kpis = manufacturing_kpis(persistence, entities=entities)

    assert kpis.completion is True
    assert kpis.lead_time_seconds == projection.lead_time_seconds
    assert kpis.output_quantity == pytest.approx(8.0)
    assert kpis.yield_ratio == pytest.approx(1.0)
    assert kpis.transition_count > 0
    assert kpis.rework_count == 0
    assert kpis.breakdown_count == 0

    assert set(asdict(kpis)) == {
        "completion",
        "lead_time_seconds",
        "output_quantity",
        "yield_ratio",
        "transition_count",
        "rework_count",
        "breakdown_count",
    }


def test_projection_exposes_incomplete_state_without_inventing_terminal_metrics() -> None:
    persistence = MemoryPersistence()
    entities = seed_happy_path(persistence, quantity=5.0)

    projection = manufacturing_projection(persistence, entities=entities)
    kpis = manufacturing_kpis(persistence, entities=entities)

    assert projection.order_state == "planned"
    assert projection.completed is False
    assert projection.finished_goods_quantity == pytest.approx(0.0)
    assert projection.lead_time_seconds is None
    assert kpis.completion is False
    assert kpis.lead_time_seconds is None
    assert kpis.yield_ratio is None



def test_quality_hold_projects_wip_without_premature_yield() -> None:
    persistence = MemoryPersistence()
    entities = seed_happy_path(persistence, quantity=5.0)
    _, engine = build_runtime(persistence)
    backend = SimPyBackend(origin=ORIGIN)
    engine.rebuild_backend(backend)
    seed_material(engine, backend, quantity=5.0)
    backend.run_until(ORIGIN.replace(hour=9))

    assert reconcile_setup_resources(
        persistence,
        engine,
        backend,
        entities=entities,
    )
    reconcile_material_issue(
        persistence,
        engine,
        backend,
        entities=entities,
        quantity=5.0,
    )
    reconcile_wip_output(
        persistence,
        engine,
        backend,
        entities=entities,
        quantity=5.0,
    )
    reconcile_quality_hold(persistence, engine, entities=entities)

    projection = manufacturing_projection(persistence, entities=entities)
    kpis = manufacturing_kpis(persistence, entities=entities)

    assert projection.order_state == "quality_hold"
    assert projection.completed is False
    assert projection.wip_item_count == 1
    assert projection.finished_goods_quantity == pytest.approx(0.0)
    assert projection.lead_time_seconds is None
    assert kpis.output_quantity == pytest.approx(0.0)
    assert kpis.yield_ratio is None
    assert kpis.completion is False


def test_manufacturing_pc5_observability_evidence_is_complete() -> None:
    manifest = process_manifest()

    assert manifest.maturity is ProcessMaturity.PC5_OBSERVABLE
    assert manifest.missing_for(ProcessMaturity.PC5_OBSERVABLE) == frozenset()
    assert manifest.kpis == frozenset(
        {
            "completion",
            "lead_time_seconds",
            "output_quantity",
            "yield_ratio",
            "transition_count",
            "rework_count",
            "breakdown_count",
        }
    )
    for evidence in (
        ProcessEvidence.KPIS,
        ProcessEvidence.PROJECTION_CONTRACT,
        ProcessEvidence.CONFIGURATION_DOCUMENTATION,
    ):
        assert evidence in manifest.evidence
        assert manifest.evidence_sources[evidence]

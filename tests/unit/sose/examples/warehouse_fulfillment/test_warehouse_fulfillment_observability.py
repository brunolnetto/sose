from __future__ import annotations

from sose.examples.process_manifest import ProcessMaturity
from sose.examples.warehouse_fulfillment.observability import (
    warehouse_fulfillment_kpis,
    warehouse_fulfillment_projection,
)
from sose.examples.warehouse_fulfillment.process_audit import process_manifest
from sose.examples.warehouse_fulfillment.simulation import run_happy_path


def test_warehouse_fulfillment_projection_and_kpis_are_read_only_and_complete() -> None:
    persistence, entities = run_happy_path()

    before = tuple(
        (entity.entity_type, entity.id, entity.state, entity.version)
        for entity in persistence.entities()
    )
    projection = warehouse_fulfillment_projection(
        persistence,
        entities=entities,
    )
    kpis = warehouse_fulfillment_kpis(
        persistence,
        entities=entities,
    )
    after = tuple(
        (entity.entity_type, entity.id, entity.state, entity.version)
        for entity in persistence.entities()
    )

    assert before == after
    assert projection.order_state == "shipped"
    assert projection.completed
    assert projection.requested_quantity == 10.0
    assert projection.allocated_quantity == 10.0
    assert projection.picked_quantity == 10.0
    assert projection.allocation_count == 2
    assert projection.substitution_allocation_count == 1
    assert projection.inventory_occurrence_count >= 4
    assert kpis.completion
    assert kpis.fulfilled_quantity == 10.0
    assert kpis.fill_rate == 1.0
    assert kpis.transition_count > 0
    assert kpis.substitution_count == 1
    assert kpis.correction_count == 0


def test_warehouse_fulfillment_pc5_observability_evidence_is_complete() -> None:
    manifest = process_manifest()

    assert manifest.maturity is ProcessMaturity.PC5_OBSERVABLE
    assert manifest.missing_for(ProcessMaturity.PC5_OBSERVABLE) == frozenset()
    assert manifest.assessment_complete is True
    assert manifest.kpis >= {
        "completion",
        "lead_time_seconds",
        "fill_rate",
        "transition_count",
        "substitution_count",
        "correction_count",
    }

from __future__ import annotations

from dataclasses import asdict

import pytest

from sose.examples.mro.observability import mro_kpis, mro_projection
from sose.examples.mro.process_audit import process_manifest
from sose.examples.mro.simulation import run_happy_path, seed_reference
from sose.examples.process_manifest import ProcessEvidence, ProcessMaturity
from sose.persistence.memory import MemoryPersistence


def test_mro_projection_is_read_only_and_idempotent() -> None:
    persistence, entities = run_happy_path(quantity=2.0)
    work_order = persistence.entity("work_order", entities.work_order_id)
    part_demand = persistence.entity("part_demand", entities.part_demand_id)
    assert work_order is not None and part_demand is not None
    before_versions = (work_order.version, part_demand.version)

    left = mro_projection(persistence, entities=entities)
    right = mro_projection(persistence, entities=entities)

    assert left == right
    assert left.work_order_id == entities.work_order_id
    assert left.part_demand_id == entities.part_demand_id
    assert left.work_order_state == "closed"
    assert left.part_demand_state == "consumed"
    assert left.closed is True
    assert left.cancelled is False
    assert left.planned_quantity == pytest.approx(2.0)
    assert left.remaining_spare_parts == pytest.approx(0.0)
    assert left.spare_part_lot_count == 0
    assert left.lead_time_seconds is not None
    assert left.lead_time_seconds >= 0.0

    work_order_after = persistence.entity("work_order", entities.work_order_id)
    part_demand_after = persistence.entity("part_demand", entities.part_demand_id)
    assert work_order_after is not None and part_demand_after is not None
    assert (work_order_after.version, part_demand_after.version) == before_versions


def test_mro_kpis_are_derived_from_durable_state_and_events() -> None:
    persistence, entities = run_happy_path(quantity=2.0)

    projection = mro_projection(persistence, entities=entities)
    kpis = mro_kpis(persistence, entities=entities)

    assert kpis.closure is True
    assert kpis.lead_time_seconds == projection.lead_time_seconds
    assert kpis.parts_consumed == pytest.approx(2.0)
    assert kpis.remaining_spare_parts == pytest.approx(0.0)
    assert kpis.transition_count > 0
    assert kpis.material_wait_count == 0
    assert kpis.resource_wait_count == 0
    assert kpis.interruption_count == 0

    assert set(asdict(kpis)) == {
        "closure",
        "lead_time_seconds",
        "parts_consumed",
        "remaining_spare_parts",
        "transition_count",
        "material_wait_count",
        "resource_wait_count",
        "interruption_count",
    }


def test_mro_projection_does_not_invent_terminal_metrics() -> None:
    persistence = MemoryPersistence()
    entities = seed_reference(persistence, quantity=3.0)

    projection = mro_projection(persistence, entities=entities)
    kpis = mro_kpis(persistence, entities=entities)

    assert projection.work_order_state == "planned"
    assert projection.closed is False
    assert projection.cancelled is False
    assert projection.remaining_spare_parts == pytest.approx(0.0)
    assert projection.lead_time_seconds is None
    assert kpis.closure is False
    assert kpis.lead_time_seconds is None
    assert kpis.parts_consumed == pytest.approx(0.0)


def test_mro_pc5_observability_evidence_is_complete() -> None:
    manifest = process_manifest()

    assert manifest.maturity is ProcessMaturity.PC5_OBSERVABLE
    assert manifest.missing_for(ProcessMaturity.PC5_OBSERVABLE) == frozenset()
    assert manifest.kpis == frozenset(
        {
            "closure",
            "lead_time_seconds",
            "parts_consumed",
            "remaining_spare_parts",
            "transition_count",
            "material_wait_count",
            "resource_wait_count",
            "interruption_count",
        }
    )
    for evidence in (
        ProcessEvidence.KPIS,
        ProcessEvidence.PROJECTION_CONTRACT,
        ProcessEvidence.CONFIGURATION_DOCUMENTATION,
    ):
        assert evidence in manifest.evidence
        assert manifest.evidence_sources[evidence]

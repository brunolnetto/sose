from __future__ import annotations

from sose.organizational.domain_experiment_analysis import analyze_domain_experiment
from sose.organizational.domain_experiment_conformance import run_domain_experiment_conformance
from sose.organizational.domain_experiment_runtime import run_domain_experiment
from sose.organizational.mro_reference import (
    MROReferenceDomain,
    MROExperimentEvidence,
    build_mro_preflight_plan_v1,
)


def test_mro_exposes_only_numeric_exogenous_quantity_and_a0() -> None:
    reference = MROReferenceDomain()
    assert reference.descriptor.identity.domain == "mro"
    assert tuple(p.name for p in reference.descriptor.parameters) == ("quantity",)
    assert tuple(a.capability_id for a in reference.descriptor.agency_capabilities) == ("fixed",)
    assert tuple(i.intervention_id for i in reference.interventions()) == (
        "spare_part_shortage", "emergency_preemption",
    )


def test_mro_resource_inventory_and_emergency_mechanics_are_durable() -> None:
    reference = MROReferenceDomain()
    result = run_domain_experiment(reference=reference, plan=build_mro_preflight_plan_v1(reference))
    assert result.manifest.world_count == 6
    assert result.manifest.run_count == 12
    assert all(isinstance(r.evidence, MROExperimentEvidence) for r in result.evidence)
    baseline_time_by_design = {
        run.design_index: run.observation.metrics["lead_time_seconds"]
        for run in result.runs if run.arm_id == "baseline"
    }
    for run in result.runs:
        if run.arm_id == "spare_part_shortage":
            assert run.observation.metrics["lead_time_seconds"] > baseline_time_by_design[run.design_index]
    for record in result.evidence:
        evidence = record.evidence
        assert evidence.closed
        assert evidence.work_order_state == "closed"
        assert evidence.part_demand_state == "consumed"
        assert evidence.spare_part_issue_count == 1
        assert evidence.parts_consumed == evidence.quantity
        assert evidence.remaining_spare_parts == 0.0
        if evidence.arm_id == "spare_part_shortage":
            assert evidence.material_wait_count >= 1
            assert evidence.interruption_count == 0
        elif evidence.arm_id == "emergency_preemption":
            assert evidence.interruption_count >= 1
            assert evidence.preemption_count == 1
            assert evidence.material_wait_count == 0
        else:
            assert evidence.arm_id == "baseline"
            assert evidence.material_wait_count == 0
            assert evidence.interruption_count == 0


def test_mro_same_generic_conformance_and_pairing_contract() -> None:
    reference = MROReferenceDomain()
    plan = build_mro_preflight_plan_v1(reference)
    conformance = run_domain_experiment_conformance(reference=reference, plan=plan)
    assert conformance.passed, [c.canonical_payload() for c in conformance.checks if not c.passed]
    result = run_domain_experiment(reference=reference, plan=plan)
    report = analyze_domain_experiment(reference=reference, result=result)
    assert len(report.effects) == 20
    eligible = [effect for effect in report.effects if effect.eligible]
    assert len(eligible) == 8
    assert all(effect.paired for effect in eligible)
    assert all(effect.mean_delta > 0 for effect in eligible)

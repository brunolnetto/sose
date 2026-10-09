from __future__ import annotations

from sose.organizational.domain_experiment_analysis import analyze_domain_experiment
from sose.organizational.domain_experiment_conformance import run_domain_experiment_conformance
from sose.organizational.domain_experiment_runtime import run_domain_experiment
from sose.organizational.o2c_reference import (
    O2CReferenceDomain,
    O2CExperimentEvidence,
    build_o2c_preflight_plan_v1,
)


def test_o2c_descriptor_exposes_only_exogenous_due_delay_and_fixed_agency() -> None:
    reference = O2CReferenceDomain()
    descriptor = reference.descriptor
    assert descriptor.identity.domain == "order_to_cash"
    assert tuple(item.name for item in descriptor.parameters) == ("due_delay_hours",)
    assert tuple(item.capability_id for item in descriptor.agency_capabilities) == ("fixed",)
    assert tuple(item.intervention_id for item in reference.interventions()) == (
        "partial_fulfillment", "overdue_collection",
    )


def test_o2c_three_arms_are_durable_and_observable_without_framework_special_cases() -> None:
    reference = O2CReferenceDomain()
    plan = build_o2c_preflight_plan_v1(reference)
    result = run_domain_experiment(reference=reference, plan=plan)
    assert result.manifest.world_count == 6
    assert result.manifest.run_count == 12
    assert all(isinstance(item.evidence, O2CExperimentEvidence) for item in result.evidence)
    assert all(item.evidence.collected for item in result.evidence)
    assert all(item.evidence.receivable_state == "collected" for item in result.evidence)

    baseline_by_design = {
        run.design_index: run.observation.metrics["order_to_cash_seconds"]
        for run in result.runs if run.arm_id == "baseline"
    }
    assert len(set(baseline_by_design.values())) == 2, "DOE axis must alter executable timing"

    for record in result.evidence:
        e = record.evidence
        assert e.amount > 0
        if e.arm_id == "baseline":
            assert e.partial_fulfillment_count == 0
            assert e.overdue_count == 0
            assert e.collection_case_count == 0
        elif e.arm_id == "partial_fulfillment":
            assert e.partial_fulfillment_count == 1
            assert e.overdue_count == 0
        else:
            assert e.arm_id == "overdue_collection"
            assert e.partial_fulfillment_count == 0
            assert e.overdue_count == 1
            assert e.collection_case_count == 1
            assert e.collection_escalation_count == 1


def test_o2c_generic_analysis_eligibility_and_conformance() -> None:
    reference = O2CReferenceDomain()
    plan = build_o2c_preflight_plan_v1(reference)
    conformance = run_domain_experiment_conformance(reference=reference, plan=plan)
    assert conformance.passed, [c.canonical_payload() for c in conformance.checks if not c.passed]

    result = run_domain_experiment(reference=reference, plan=plan)
    report = analyze_domain_experiment(reference=reference, result=result)
    assert len(report.effects) == 16
    eligible = [effect for effect in report.effects if effect.eligible]
    assert len(eligible) == 6
    for effect in eligible:
        assert effect.paired
        if effect.treatment_arm_id == "partial_fulfillment":
            assert effect.metric_name == "partial_fulfillment_count"
            assert effect.mean_delta == 1.0
        else:
            assert effect.treatment_arm_id == "overdue_collection"
            assert effect.metric_name in {"overdue_count", "order_to_cash_seconds"}
            assert effect.mean_delta > 0

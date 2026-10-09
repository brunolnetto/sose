"""Cross-domain Gate-A contract: one suite, three unrelated operational semantics.

These are non-official characterization experiments. Each domain retains its
own mechanism and eligibility rules; framework-level invariants are identical.
"""
from __future__ import annotations

import pytest

from sose.organizational.domain_experiment_conformance import run_domain_experiment_conformance
from sose.organizational.domain_experiment_runtime import run_domain_experiment
from sose.organizational.domain_experiment_analysis import analyze_domain_experiment
from sose.organizational.manufacturing_reference import (
    ManufacturingReferenceDomain, build_manufacturing_preflight_plan_v1,
)
from sose.organizational.o2c_reference import O2CReferenceDomain, build_o2c_preflight_plan_v1
from sose.organizational.mro_reference import MROReferenceDomain, build_mro_preflight_plan_v1


REFERENCES = (
    ("manufacturing", ManufacturingReferenceDomain, build_manufacturing_preflight_plan_v1),
    ("order_to_cash", O2CReferenceDomain, build_o2c_preflight_plan_v1),
    ("mro", MROReferenceDomain, build_mro_preflight_plan_v1),
)

GATE_A_CHECKS = {
    "descriptor-integrity",
    "deterministic-world-construction",
    "exogenous-binding",
    "deterministic-execution",
    "crn-pairing",
    "observation-completeness",
    "ground-truth-integrity",
    "regime-purity",
    "result-reproducibility",
}


@pytest.mark.parametrize("domain,reference_cls,plan_builder", REFERENCES, ids=[x[0] for x in REFERENCES])
def test_same_gatea_invariants_across_three_process_semantics(domain, reference_cls, plan_builder):
    reference = reference_cls()
    plan = plan_builder(reference)

    assert reference.descriptor.identity.domain == domain
    assert tuple(a.capability_id for a in reference.descriptor.agency_capabilities) == ("fixed",)
    assert set(plan.protocol.parameter_ranges) == {p.name for p in reference.descriptor.parameters}

    report = run_domain_experiment_conformance(reference=reference, plan=plan)
    assert {check.name for check in report.checks} == GATE_A_CHECKS
    assert report.passed, [check.canonical_payload() for check in report.checks if not check.passed]

    result_a = run_domain_experiment(reference=reference, plan=plan)
    result_b = run_domain_experiment(reference=reference, plan=plan)
    assert result_a.result_hash == result_b.result_hash
    assert result_a.manifest.world_count == plan.protocol.sample_size * 3
    assert result_a.manifest.run_count == result_a.manifest.world_count * 2
    assert all(e.evidence_hash for e in result_a.evidence)
    assert all(assessment.passed for assessment in result_a.assessments if assessment.eligible)

    analysis = analyze_domain_experiment(reference=reference, result=result_a)
    assert any(effect.eligible for effect in analysis.effects)
    assert any(not effect.eligible for effect in analysis.effects)


def test_references_are_not_collapsed_into_one_generic_business_model():
    references = [cls() for _, cls, _ in REFERENCES]
    identities = [r.descriptor.identity.reference_hash for r in references]
    assert len(set(identities)) == len(identities)
    arms = [tuple(i.intervention_id for i in r.interventions()) for r in references]
    assert len(set(arms)) == len(arms)

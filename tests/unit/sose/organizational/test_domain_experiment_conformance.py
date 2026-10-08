from __future__ import annotations

from sose.organizational.domain_experiment_conformance import (
    DomainExperimentConformance,
    run_domain_experiment_conformance,
)
from sose.organizational.synthetic_queue_reference import (
    QueueReferenceDomain,
    build_queue_reference_plan_v1,
)


def test_queue_reference_passes_gate_a_conformance() -> None:
    reference = QueueReferenceDomain()
    report = run_domain_experiment_conformance(
        reference=reference,
        plan=build_queue_reference_plan_v1(reference),
    )

    assert isinstance(report, DomainExperimentConformance)
    assert report.passed
    assert report.checks
    assert all(check.passed for check in report.checks)


def test_conformance_is_deterministic_and_hash_addressed() -> None:
    reference = QueueReferenceDomain()
    plan = build_queue_reference_plan_v1(reference)

    left = run_domain_experiment_conformance(reference=reference, plan=plan)
    right = run_domain_experiment_conformance(reference=reference, plan=plan)

    assert left == right
    assert left.report_hash == right.report_hash
    assert len(left.report_hash) == 64


def test_conformance_checks_expected_gate_a_invariants() -> None:
    report = run_domain_experiment_conformance(
        reference=QueueReferenceDomain(),
        plan=build_queue_reference_plan_v1(),
    )

    names = {check.name for check in report.checks}
    assert {
        "descriptor-integrity",
        "deterministic-world-construction",
        "exogenous-binding",
        "deterministic-execution",
        "crn-pairing",
        "observation-completeness",
        "ground-truth-integrity",
        "regime-purity",
        "result-reproducibility",
    } <= names

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


def test_conformance_executes_every_arm_for_each_agency_level() -> None:
    class CountingQueueReference(QueueReferenceDomain):
        def __init__(self) -> None:
            self.seen: set[tuple[str, str]] = set()

        def execute(self, request):
            self.seen.add(
                (request.world.agency_configuration.level.value, request.world.arm_id)
            )
            return super().execute(request)

    reference = CountingQueueReference()
    report = run_domain_experiment_conformance(
        reference=reference,
        plan=build_queue_reference_plan_v1(reference),
    )

    assert report.passed
    assert reference.seen == {
        ("A0", "baseline"),
        ("A0", "capacity-up"),
        ("A0", "automation-rework"),
        ("A1", "baseline"),
        ("A1", "capacity-up"),
        ("A1", "automation-rework"),
    }


def test_conformance_rejects_broken_crn_latent_signature() -> None:
    class BrokenCrnReference(QueueReferenceDomain):
        def crn_signature(self, result):
            signature = super().crn_signature(result)
            evidence = result.evidence
            return (getattr(evidence, "arm_id", "unknown"), signature)

    report = run_domain_experiment_conformance(
        reference=BrokenCrnReference(),
        plan=build_queue_reference_plan_v1(),
    )

    check = next(item for item in report.checks if item.name == "crn-pairing")
    assert not check.passed


def test_conformance_rejects_regime_classifier_that_depends_on_execution_state() -> None:
    class StatefulRegimeReference(QueueReferenceDomain):
        def __init__(self) -> None:
            self.executed = False

        def execute(self, request):
            self.executed = True
            return super().execute(request)

        def classify_regime(self, world):
            value = super().classify_regime(world)
            if not self.executed:
                return value
            return value.model_copy(update={"label": f"post-{value.label}"})

    report = run_domain_experiment_conformance(
        reference=StatefulRegimeReference(),
        plan=build_queue_reference_plan_v1(),
    )

    check = next(item for item in report.checks if item.name == "regime-purity")
    assert not check.passed


def test_conformance_rejects_eligible_metric_claim_missing_from_observation() -> None:
    class MissingMetricReference(QueueReferenceDomain):
        def observation(self, result):
            observation = super().observation(result)
            metrics = dict(observation.metrics)
            metrics.pop("mean_lead_time", None)
            metrics.pop("lead_time", None)
            return observation.model_copy(update={"metrics": metrics})

    report = run_domain_experiment_conformance(
        reference=MissingMetricReference(),
        plan=build_queue_reference_plan_v1(),
    )

    check = next(item for item in report.checks if item.name == "ground-truth-integrity")
    assert not check.passed

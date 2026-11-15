"""Generic fixed-replication Gate-A experiment orchestration."""
from __future__ import annotations

from collections.abc import Callable
from functools import partial
from typing import Protocol

from pydantic import BaseModel

from sose.core.randomness import scoped_seed

from .domain_experiment import (
    AgencyConfiguration,  # noqa: F401 - legacy module surface
    ExperimentWorld,
    build_experiment_worlds,
)
from .domain_experiment_records import (  # noqa: F401 - re-exported public/legacy surface
    DomainEvidenceRecord,
    DomainExecutionRequest,
    DomainExecutionResult,
    DomainExperimentPlan,
    DomainExperimentResult,
    ExperimentObservation,
    ExperimentResultManifest,
    ExperimentRunRecord,
    GroundTruthAssessment,
    NonBlankString,
    RegimeReference,
    WorldReferenceRecord,
    _canonical_hash,
    _freeze_json,
    _thaw_json,
    _validate_json,
    _validate_sha256,
)
from .domain_reference import (
    DomainReferenceDescriptor,
    GroundTruthClaim,
    GroundTruthComparisonKind,
    GroundTruthTargetKind,
)
from .experiment import ExperimentProtocol  # noqa: F401 - legacy module surface
from .model_spec import ModelIntervention, ModelSpec


class _RuntimeDomainReference(Protocol):
    descriptor: DomainReferenceDescriptor

    def build_model(self, point: dict[str, float]) -> ModelSpec: ...

    def interventions(self) -> tuple[ModelIntervention, ...]: ...

    def execute(self, request: DomainExecutionRequest) -> DomainExecutionResult: ...

    def observation(self, result: DomainExecutionResult) -> ExperimentObservation: ...

    def ground_truth(self, world: ExperimentWorld) -> tuple[GroundTruthClaim, ...]: ...

    def classify_regime(self, world: ExperimentWorld) -> RegimeReference: ...


def run_domain_experiment(
    *,
    reference: _RuntimeDomainReference,
    plan: DomainExperimentPlan,
) -> DomainExperimentResult:
    """Execute one fixed-replication Gate-A experiment through generic orchestration."""

    replication_plan = plan.protocol.replication_plan
    if replication_plan.min_replications != replication_plan.max_replications:
        raise ValueError("Gate-A runtime requires a fixed replication plan")

    worlds = build_experiment_worlds(
        reference=reference,
        protocol=plan.protocol,
        agency_configurations=plan.agency_configurations,
        design_seed=plan.design_seed,
    )
    references = tuple(
        WorldReferenceRecord(
            world_hash=world.world_hash,
            regime=reference.classify_regime(world),
            ground_truth=reference.ground_truth(world),
        )
        for world in worlds
    )
    reference_by_world = {
        record.world_hash: record
        for record in references
    }

    runs: list[ExperimentRunRecord] = []
    evidence_records: list[DomainEvidenceRecord] = []
    assessments: list[GroundTruthAssessment] = []
    for world in worlds:
        world_reference = reference_by_world[world.world_hash]
        for replication in range(replication_plan.min_replications):
            _run_replication(
                reference=reference, plan=plan, world=world,
                world_reference=world_reference, replication=replication,
                runs=runs, evidence_records=evidence_records, assessments=assessments,
            )

    run_tuple = tuple(runs)
    evidence_tuple = tuple(evidence_records)
    assessment_tuple = tuple(assessments)
    result_hash = _canonical_hash(
        {
            "plan": plan.canonical_payload(),
            "worlds": [world.canonical_payload() for world in worlds],
            "runs": [run.canonical_payload() for run in run_tuple],
            "references": [record.canonical_payload() for record in references],
            "assessments": [
                assessment.canonical_payload()
                for assessment in assessment_tuple
            ],
        }
    )
    manifest = ExperimentResultManifest(
        plan_hash=plan.plan_hash,
        protocol_hash=plan.protocol.protocol_hash,
        domain_reference_hash=reference.descriptor.descriptor_hash,
        world_count=len(worlds),
        run_count=len(run_tuple),
        result_hash=result_hash,
    )
    return DomainExperimentResult(
        plan=plan,
        worlds=worlds,
        runs=run_tuple,
        evidence=evidence_tuple,
        references=references,
        assessments=assessment_tuple,
        manifest=manifest,
    )


def _run_replication(
    *,
    reference: _RuntimeDomainReference,
    plan: DomainExperimentPlan,
    world: ExperimentWorld,
    world_reference: WorldReferenceRecord,
    replication: int,
    runs: list[ExperimentRunRecord],
    evidence_records: list[DomainEvidenceRecord],
    assessments: list[GroundTruthAssessment],
) -> None:
    """Execute one replication of one world and append its records in order."""
    execution = reference.execute(
        DomainExecutionRequest(
            protocol=plan.protocol,
            world=world,
            root_seed=plan.root_seed,
            replication=replication,
        )
    )
    observation = reference.observation(execution)
    evidence_records.append(
        DomainEvidenceRecord(
            world_hash=world.world_hash,
            replication=replication,
            evidence_hash=execution.evidence_hash,
            evidence=execution.evidence,
        )
    )
    runs.append(
        ExperimentRunRecord(
            world_hash=world.world_hash,
            design_index=world.design_index,
            arm_id=world.arm_id,
            agency_level=world.agency_configuration.level.value,
            replication=replication,
            replication_seed=scoped_seed(
                plan.root_seed,
                world.crn_group,
                replication,
            ),
            evidence_hash=execution.evidence_hash,
            observation=observation,
        )
    )
    assessments.extend(
        _assess_claim(
            world_hash=world.world_hash,
            replication=replication,
            claim=claim,
            observation=observation,
            regime=world_reference.regime,
        )
        for claim in world_reference.ground_truth
    )


def evidence_hash(payload: object) -> str:
    """Hash JSON-serializable raw evidence without standardizing its domain schema."""

    if isinstance(payload, BaseModel):
        value = payload.model_dump(mode="json")
    elif hasattr(payload, "canonical_payload"):
        value = payload.canonical_payload()
    else:
        value = payload
    _validate_json(value, path="/evidence")
    return _canonical_hash(value)


def _assess_claim(
    *,
    world_hash: str,
    replication: int,
    claim: GroundTruthClaim,
    observation: ExperimentObservation,
    regime: RegimeReference,
) -> GroundTruthAssessment:
    assess = partial(
        GroundTruthAssessment,
        world_hash=world_hash,
        replication=replication,
        claim_id=claim.claim_id,
        claim_hash=claim.claim_hash,
    )
    if not claim.eligible:
        return assess(eligible=False, reason=claim.ineligibility_reason)
    if claim.target_kind is GroundTruthTargetKind.REGIME:
        observed_regime = regime.label
        if (
            claim.comparison_rule.kind is not GroundTruthComparisonKind.EXACT
            or not isinstance(claim.expected, str)
        ):
            return assess(
                eligible=True, observed=observed_regime,
                reason="regime claims require an exact string expectation",
            )
        return assess(
            eligible=True, observed=observed_regime,
            passed=observed_regime == claim.expected,
        )
    if claim.target_kind is not GroundTruthTargetKind.METRIC:
        return assess(
            eligible=True, reason="claim target is not assessed by the Gate-A runtime",
        )
    return _assess_metric(assess, claim, observation.metrics.get(claim.target_name))


def _assess_metric(
    assess: Callable[..., GroundTruthAssessment],
    claim: GroundTruthClaim,
    observed: float | None,
) -> GroundTruthAssessment:
    if observed is None:
        return assess(
            eligible=True,
            reason="standard observation does not expose the claimed metric",
        )
    expected = claim.expected
    if isinstance(expected, bool) or not isinstance(expected, (int, float)):
        return assess(
            eligible=True, observed=observed,
            reason="metric claim expected value is not numeric",
        )
    expected_value = float(expected)
    rule = claim.comparison_rule
    if rule.kind is GroundTruthComparisonKind.EXACT:
        passed = observed == expected_value
    elif rule.kind is GroundTruthComparisonKind.ABSOLUTE_TOLERANCE:
        assert rule.tolerance is not None
        passed = abs(observed - expected_value) <= rule.tolerance
    elif rule.kind is GroundTruthComparisonKind.RELATIVE_TOLERANCE:
        assert rule.tolerance is not None
        passed = abs(observed - expected_value) / abs(expected_value) <= rule.tolerance
    elif rule.kind is GroundTruthComparisonKind.LOWER_BOUND:
        passed = observed >= expected_value
    else:
        passed = observed <= expected_value
    return assess(eligible=True, observed=observed, passed=passed)

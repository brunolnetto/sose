from __future__ import annotations

from collections.abc import Mapping
from functools import cached_property, lru_cache

from .agency import A1Policy, A1PolicyName, AgencyLevel, AgencySpec
from .domain_experiment import AgencyConfiguration, ExperimentWorld
from .domain_experiment_runtime import (
    DomainExecutionRequest,
    DomainExecutionResult,
    DomainExperimentPlan,
    ExperimentObservation,
    RegimeReference,
    evidence_hash,
)
from .domain_reference import (
    AgencyCapabilitySpec,
    DomainReferenceDescriptor,
    DomainReferenceIdentity,
    GroundTruthClaim,
    GroundTruthComparisonKind,
    GroundTruthComparisonRule,
    GroundTruthKind,
    GroundTruthTargetKind,
    ParameterDefinition,
)
from .experiment import ParameterRange
from .model_spec import ModelIntervention, ModelSpec
from .synthetic_a1 import A1AdaptiveRunResult, run_a1_reference_world
from .synthetic_a1_experiment import (
    A1RegimeKind,
    build_a0_a1_reference_design_v1,
    classify_a1_regime,
)
from .synthetic_analysis import analytical_world_reference
from .synthetic_runner import SyntheticRunResult, run_a0_reference_world
from .synthetic_study import SyntheticWorldSpec


class QueueReferenceDomain:
    """Gate-A compatibility adapter for the already verified queue reference."""

    @cached_property
    def descriptor(self) -> DomainReferenceDescriptor:
        design = _legacy_design()
        baseline = design.baseline
        metadata = {
            "arrival_rate": ("Configured exogenous arrival intensity.", "items/time"),
            "service_capacity": ("Configured nominal service capacity.", "items/time"),
            "service_cv": ("Configured service-time coefficient of variation.", None),
            "rework_probability": ("Configured probability of one rework cycle.", None),
            "transition_cost": ("Configured uncertain intervention transition cost.", "cost"),
        }
        return DomainReferenceDescriptor(
            identity=DomainReferenceIdentity(
                domain="organizational-queue-reference",
                reference_id="synthetic-queue-v1",
                reference_version="1",
                process_manifest_domain="synthetic-reference-fixture",
                specification_path="docs/organizational/evidence/synthetic-a1-reference-v1/RESULT.md",
            ),
            parameters=tuple(
                ParameterDefinition(
                    name=name,
                    description=metadata[name][0],
                    units=metadata[name][1],
                    default=float(baseline.parameters[name]),
                    range=design.protocol.parameter_ranges[name],
                    evidence_class=baseline.parameter_evidence[name],
                )
                for name in sorted(design.protocol.parameter_ranges)
            ),
            agency_capabilities=(
                AgencyCapabilitySpec(
                    level=AgencyLevel.A0,
                    capability_id="fixed",
                ),
                AgencyCapabilitySpec(
                    level=AgencyLevel.A1,
                    capability_id="batching",
                    parameter_ranges={
                        "backlog_trigger": ParameterRange(low=0.0, high=50.0),
                        "service_multiplier": ParameterRange(low=0.1, high=1.0),
                        "adaptation_time": ParameterRange(low=0.0, high=5.0),
                        "adaptation_cost_rate": ParameterRange(low=0.0, high=10.0),
                    },
                    defaults={
                        "backlog_trigger": 2.0,
                        "service_multiplier": 0.70,
                        "adaptation_time": 0.05,
                        "adaptation_cost_rate": 0.15,
                    },
                    compatibility_constraints=(
                        "fixed structural envelope",
                        "local backlog observation only",
                    ),
                ),
            ),
            ground_truth_kinds=(
                GroundTruthKind.ANALYTICAL,
                GroundTruthKind.MECHANISTIC,
                GroundTruthKind.INVARIANT,
            ),
        )

    def build_model(self, point: dict[str, float]) -> ModelSpec:
        payload = _legacy_design().baseline.canonical_payload()
        parameters = dict(payload["parameters"])
        parameters.update({name: float(value) for name, value in point.items()})
        payload["parameters"] = parameters
        return ModelSpec.model_validate(payload)

    def interventions(self) -> tuple[ModelIntervention, ...]:
        return _legacy_design().interventions

    def execute(self, request: DomainExecutionRequest) -> DomainExecutionResult:
        legacy = _legacy_world(request.world)
        if request.world.agency_configuration.level is AgencyLevel.A0:
            evidence = run_a0_reference_world(
                protocol=request.protocol,
                world=legacy,
                root_seed=request.root_seed,
                replication=request.replication,
            )
        elif request.world.agency_configuration.level is AgencyLevel.A1:
            evidence = run_a1_reference_world(
                protocol=request.protocol,
                world=legacy,
                root_seed=request.root_seed,
                replication=request.replication,
            )
        else:
            raise ValueError("queue reference supports A0 and A1 only")
        return DomainExecutionResult(
            evidence_hash=evidence_hash(evidence),
            evidence=evidence,
        )

    def observation(self, result: DomainExecutionResult) -> ExperimentObservation:
        evidence = result.evidence
        if not isinstance(evidence, (SyntheticRunResult, A1AdaptiveRunResult)):
            raise TypeError("queue reference received unsupported execution evidence")
        metrics = {
            "mean_lead_time": evidence.mean_lead_time,
            "median_lead_time": evidence.median_lead_time,
            "p90_lead_time": evidence.p90_lead_time,
            "throughput": evidence.throughput,
            "mean_wip": evidence.mean_wip,
            "rework_fraction": evidence.rework_fraction,
            "total_operating_cost": evidence.total_operating_cost,
        }
        if isinstance(evidence, A1AdaptiveRunResult):
            metrics.update(
                {
                    "adaptation_count": float(evidence.adaptation_count),
                    "adaptation_actor_time": evidence.adaptation_actor_time_measurement,
                    "adaptation_cost": evidence.adaptation_cost,
                }
            )
        return ExperimentObservation(metrics=metrics)

    def ground_truth(self, world: ExperimentWorld) -> tuple[GroundTruthClaim, ...]:
        legacy = _legacy_world(world)
        if world.agency_configuration.level is AgencyLevel.A0:
            reference = analytical_world_reference(legacy)
            return (
                GroundTruthClaim(
                    claim_id="a0.configured-regime",
                    kind=GroundTruthKind.MECHANISTIC,
                    target_kind=GroundTruthTargetKind.REGIME,
                    target_name="queue_regime",
                    expected="stable" if reference.stable else "saturated",
                    comparison_rule=GroundTruthComparisonRule(
                        kind=GroundTruthComparisonKind.EXACT
                    ),
                    assumptions=("configured queue mechanics define offered load",),
                    eligibility_rule="always",
                    eligible=True,
                    provenance=(
                        "sose.organizational.synthetic_analysis.analytical_world_reference",
                    ),
                ),
                GroundTruthClaim(
                    claim_id="a0.mean-lead-time",
                    kind=GroundTruthKind.ANALYTICAL,
                    target_kind=GroundTruthTargetKind.METRIC,
                    target_name="mean_lead_time",
                    expected=reference.expected_mean_lead_time,
                    comparison_rule=(
                        GroundTruthComparisonRule(
                            kind=GroundTruthComparisonKind.RELATIVE_TOLERANCE,
                            tolerance=0.20,
                        )
                        if reference.stable
                        else GroundTruthComparisonRule(
                            kind=GroundTruthComparisonKind.EXACT
                        )
                    ),
                    assumptions=(
                        "stationary M/G/1 reference mechanics",
                        "configured arrival and service mechanics define the reference",
                    ),
                    eligibility_rule="offered_load < 1",
                    eligible=reference.stable,
                    ineligibility_reason=(
                        None
                        if reference.stable
                        else "stationary mean lead time is undefined for saturated worlds"
                    ),
                    provenance=(
                        "sose.organizational.synthetic_analysis.analytical_world_reference",
                    ),
                ),
            )

        reference = classify_a1_regime(legacy)
        return (
            GroundTruthClaim(
                claim_id="a1.configured-regime",
                kind=GroundTruthKind.MECHANISTIC,
                target_kind=GroundTruthTargetKind.REGIME,
                target_name="queue_regime",
                expected=reference.kind.value,
                comparison_rule=GroundTruthComparisonRule(
                    kind=GroundTruthComparisonKind.EXACT
                ),
                assumptions=(
                    "backlog-triggered batching remains inside the fixed structural envelope",
                    "adapted workload drift determines A1 stability",
                ),
                eligibility_rule="always",
                eligible=True,
                provenance=(
                    "sose.organizational.synthetic_a1_experiment.classify_a1_regime",
                ),
            ),
        )

    def classify_regime(self, world: ExperimentWorld) -> RegimeReference:
        legacy = _legacy_world(world)
        if world.agency_configuration.level is AgencyLevel.A0:
            reference = analytical_world_reference(legacy)
            return RegimeReference(
                label="stable" if reference.stable else "saturated",
                stable=reference.stable,
                metadata={
                    "offered_load": reference.offered_load,
                    "service_ceiling": reference.service_ceiling,
                },
            )

        reference = classify_a1_regime(legacy)
        return RegimeReference(
            label=reference.kind.value,
            stable=reference.kind is not A1RegimeKind.SATURATED,
            metadata={
                "nominal_load": reference.nominal_load,
                "adapted_load": reference.adapted_load,
            },
        )


def build_queue_reference_plan_v1(
    reference: QueueReferenceDomain | None = None,
) -> DomainExperimentPlan:
    """Expose the verified A0/A1 protocol through the generic runtime contract."""

    reference = reference or QueueReferenceDomain()
    design = _legacy_design()
    return DomainExperimentPlan(
        protocol=design.protocol,
        design_seed=design.design_seed,
        root_seed=design.root_seed,
        agency_configurations=(
            AgencyConfiguration(
                level=AgencyLevel.A0,
                capability_id="fixed",
            ),
            AgencyConfiguration(
                level=AgencyLevel.A1,
                capability_id="batching",
                parameters={
                    "backlog_trigger": 2.0,
                    "service_multiplier": 0.70,
                    "adaptation_time": 0.05,
                    "adaptation_cost_rate": 0.15,
                },
            ),
        ),
    )


@lru_cache(maxsize=1)
def _legacy_design():
    return build_a0_a1_reference_design_v1()


def _agency_spec(configuration: AgencyConfiguration) -> AgencySpec:
    if configuration.level is AgencyLevel.A0:
        if configuration.capability_id != "fixed" or configuration.parameters:
            raise ValueError("A0 queue reference supports only fixed agency")
        return AgencySpec(level=AgencyLevel.A0)
    if (
        configuration.level is AgencyLevel.A1
        and configuration.capability_id == "batching"
    ):
        return AgencySpec(
            level=AgencyLevel.A1,
            a1_policy=A1Policy(
                name=A1PolicyName.BATCHING,
                parameters=dict(configuration.parameters),
            ),
        )
    raise ValueError("unsupported queue reference agency configuration")


def _legacy_world(world: ExperimentWorld) -> SyntheticWorldSpec:
    payload = world.model_spec.canonical_payload()
    payload["agency"] = _agency_spec(world.agency_configuration).canonical_payload()
    model_spec = ModelSpec.model_validate(payload)
    return SyntheticWorldSpec(
        design_index=world.design_index,
        arm_id=world.arm_id,
        agency_level=world.agency_configuration.level,
        crn_group=world.crn_group,
        protocol_hash=world.protocol_hash,
        baseline_model_spec_hash=world.baseline_model_spec_hash,
        exogenous_parameters=dict(world.exogenous_parameters),
        intervention_class=world.intervention_class,
        intervention_operating_cost=world.intervention_operating_cost,
        intervention_transition_cost=world.intervention_transition_cost,
        intervention_transition_time=world.intervention_transition_time,
        model_spec=model_spec,
        model_spec_hash=model_spec.model_spec_hash,
    )

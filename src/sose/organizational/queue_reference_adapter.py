from __future__ import annotations

from functools import cached_property

from .agency import AgencyLevel
from .domain_reference import (
    AgencyCapabilitySpec,
    DomainReferenceDescriptor,
    DomainReferenceIdentity,
    ExperimentObservation,
    GroundTruthClaim,
    GroundTruthComparisonKind,
    GroundTruthComparisonRule,
    GroundTruthKind,
    GroundTruthTargetKind,
    ParameterDefinition,
    ParameterSpaceSpec,
)
from .experiment import ExperimentProtocol, ParameterRange
from .synthetic_a1 import A1AdaptiveRunResult, run_a1_reference_world
from .synthetic_a1_experiment import A1RegimeReference, classify_a1_regime
from .synthetic_analysis import AnalyticalWorldReference, analytical_world_reference
from .synthetic_reference_experiment import build_reference_synthetic_experiment_v1
from .synthetic_runner import SyntheticRunResult, run_a0_reference_world
from .synthetic_study import SyntheticWorldSpec


class QueueReferenceAdapter:
    """Compatibility adapter for the verified queue A0/A1 reference.

    This remains a provisional organizational-layer reference. It is not a promoted
    business-process canonical and does not bypass the PC5/PC6 gates in TRD-0001.
    """

    @cached_property
    def identity(self) -> DomainReferenceIdentity:
        return DomainReferenceIdentity(
            domain="synthetic_queue_reference",
            reference_id="synthetic-queue-reference",
            reference_version="1",
            process_manifest_domain="synthetic_queue_reference",
            specification_path=(
                "docs/organizational/evidence/synthetic-reference-v1/RESULT.md"
            ),
        )

    def parameter_space(self) -> ParameterSpaceSpec:
        design = build_reference_synthetic_experiment_v1()
        descriptions = {
            "arrival_rate": "Exogenous item arrival intensity.",
            "service_capacity": "Configured nominal service capacity.",
            "service_cv": "Configured service-time coefficient of variation.",
            "rework_probability": "Configured probability of one rework cycle.",
            "transition_cost": "Configured intervention transition cost coordinate.",
        }
        units = {
            "arrival_rate": "items/time",
            "service_capacity": "items/time",
            "service_cv": "ratio",
            "rework_probability": "probability",
            "transition_cost": "cost",
        }
        return ParameterSpaceSpec(
            parameters=tuple(
                ParameterDefinition(
                    name=name,
                    description=descriptions[name],
                    units=units[name],
                    default=float(design.baseline.parameters[name]),
                    range=bounds,
                    evidence_class=design.baseline.parameter_evidence[name],
                )
                for name, bounds in sorted(design.protocol.parameter_ranges.items())
            )
        )

    def agency_capabilities(self) -> tuple[AgencyCapabilitySpec, ...]:
        return (
            AgencyCapabilitySpec(
                level=AgencyLevel.A0,
                capability_id="fixed-policy",
            ),
            AgencyCapabilitySpec(
                level=AgencyLevel.A1,
                capability_id="batching",
                parameter_ranges={
                    "backlog_trigger": ParameterRange(low=0.0, high=1_000_000.0),
                    "service_multiplier": ParameterRange(low=0.000001, high=1.0),
                    "adaptation_time": ParameterRange(low=0.0, high=1_000_000.0),
                    "adaptation_cost_rate": ParameterRange(low=0.0, high=1_000_000.0),
                },
                defaults={
                    "backlog_trigger": 2.0,
                    "service_multiplier": 0.70,
                    "adaptation_time": 0.05,
                    "adaptation_cost_rate": 0.15,
                },
                compatibility_constraints=(
                    "does not change nominal organizational structure",
                    "does not change demand generation",
                    "does not change authority topology",
                ),
            ),
        )

    def descriptor(self) -> DomainReferenceDescriptor:
        space = self.parameter_space()
        return DomainReferenceDescriptor(
            identity=self.identity,
            parameters=space.parameters,
            agency_capabilities=self.agency_capabilities(),
            ground_truth_kinds=(
                GroundTruthKind.ANALYTICAL,
                GroundTruthKind.MECHANISTIC,
            ),
        )

    def classify_regime(
        self,
        world: SyntheticWorldSpec,
    ) -> AnalyticalWorldReference | A1RegimeReference:
        if world.agency_level is AgencyLevel.A0:
            return analytical_world_reference(world)
        if world.agency_level is AgencyLevel.A1:
            return classify_a1_regime(world)
        raise ValueError("queue reference adapter supports A0 and A1 only")

    def ground_truth(
        self,
        world: SyntheticWorldSpec,
    ) -> tuple[GroundTruthClaim, ...]:
        if world.agency_level is AgencyLevel.A0:
            reference = analytical_world_reference(world)
            equivalence_margin = next(
                outcome.equivalence_margin
                for outcome in build_reference_synthetic_experiment_v1().protocol.outcomes
                if outcome.name == "lead_time"
            )
            regime = GroundTruthClaim(
                claim_id="queue.regime",
                kind=GroundTruthKind.MECHANISTIC,
                target_kind=GroundTruthTargetKind.REGIME,
                target_name="stability",
                expected="stable" if reference.stable else "saturated",
                comparison_rule=GroundTruthComparisonRule(
                    kind=GroundTruthComparisonKind.EXACT,
                ),
                assumptions=(
                    "Configured arrival, service, and rework mechanics are exogenous.",
                ),
                eligibility_rule="configured offered load determines regime",
                eligible=True,
                provenance=(
                    "src/sose/organizational/synthetic_analysis.py:analytical_world_reference",
                ),
            )
            lead = GroundTruthClaim(
                claim_id="queue.mean_lead_time",
                kind=GroundTruthKind.ANALYTICAL,
                target_kind=GroundTruthTargetKind.METRIC,
                target_name="mean_lead_time",
                expected=reference.expected_mean_lead_time,
                comparison_rule=(
                    GroundTruthComparisonRule(
                        kind=GroundTruthComparisonKind.RELATIVE_TOLERANCE,
                        tolerance=equivalence_margin,
                    )
                    if reference.expected_mean_lead_time is not None
                    else GroundTruthComparisonRule(
                        kind=GroundTruthComparisonKind.EXACT,
                    )
                ),
                assumptions=(
                    "Stationary M/G/1 mean lead time is valid only for offered load below one.",
                    "Service and rework moments follow the configured queue reference mechanics.",
                ),
                eligibility_rule="offered_load < 1",
                eligible=reference.expected_mean_lead_time is not None,
                ineligibility_reason=(
                    None
                    if reference.expected_mean_lead_time is not None
                    else "stationary mean lead time is undefined for saturated worlds"
                ),
                provenance=(
                    "src/sose/organizational/synthetic_analysis.py:analytical_world_reference",
                ),
            )
            return (regime, lead)

        if world.agency_level is AgencyLevel.A1:
            reference = classify_a1_regime(world)
            return (
                GroundTruthClaim(
                    claim_id="queue.a1_regime",
                    kind=GroundTruthKind.MECHANISTIC,
                    target_kind=GroundTruthTargetKind.REGIME,
                    target_name="a1_stability",
                    expected=reference.kind.value,
                    comparison_rule=GroundTruthComparisonRule(
                        kind=GroundTruthComparisonKind.EXACT,
                    ),
                    assumptions=(
                        "A1 regime is derived from configured nominal load and local adaptation mechanics.",
                        "No realized WIP, lead time, throughput, or backlog outcome defines the regime.",
                    ),
                    eligibility_rule="configured A1 policy and load mechanics are valid",
                    eligible=True,
                    provenance=(
                        "src/sose/organizational/synthetic_a1_experiment.py:classify_a1_regime",
                    ),
                ),
            )

        raise ValueError("queue reference adapter supports A0 and A1 only")

    def execute(
        self,
        *,
        protocol: ExperimentProtocol,
        world: SyntheticWorldSpec,
        root_seed: int,
        replication: int,
    ) -> SyntheticRunResult | A1AdaptiveRunResult:
        if world.agency_level is AgencyLevel.A0:
            return run_a0_reference_world(
                protocol=protocol,
                world=world,
                root_seed=root_seed,
                replication=replication,
            )
        if world.agency_level is AgencyLevel.A1:
            return run_a1_reference_world(
                protocol=protocol,
                world=world,
                root_seed=root_seed,
                replication=replication,
            )
        raise ValueError("queue reference adapter supports A0 and A1 only")

    def observation(
        self,
        result: SyntheticRunResult | A1AdaptiveRunResult,
    ) -> ExperimentObservation:
        metrics = {
            "throughput": result.throughput,
            "mean_wip": result.mean_wip,
            "mean_lead_time": result.mean_lead_time,
            "median_lead_time": result.median_lead_time,
            "p90_lead_time": result.p90_lead_time,
            "rework_fraction": result.rework_fraction,
            "total_operating_cost": result.total_operating_cost,
        }
        if isinstance(result, A1AdaptiveRunResult):
            metrics.update(
                {
                    "adaptation_actor_time": result.adaptation_actor_time_measurement,
                    "adaptation_cost": result.adaptation_cost,
                }
            )
        return ExperimentObservation(
            world_hash=result.world_hash,
            replication=result.replication,
            metrics=metrics,
        )

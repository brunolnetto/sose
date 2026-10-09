from __future__ import annotations

from functools import cached_property

from .agency import AgencyLevel
from .domain_reference import (
    AgencyCapabilitySpec,
    AgencyConfigurationSpec,
    AgencyParameterSpec,
    DomainReferenceIdentity,
    ExperimentObservation,
    GroundTruthClaim,
    GroundTruthKind,
    GroundTruthTargetKind,
    ParameterDefinition,
    ParameterSpaceSpec,
)
from .experiment import ExperimentProtocol
from .synthetic_a1 import A1AdaptiveRunResult, run_a1_reference_world
from .synthetic_a1_experiment import classify_a1_regime
from .synthetic_analysis import AnalyticalWorldReference, analytical_world_reference
from .synthetic_reference_experiment import build_reference_synthetic_experiment_v1
from .synthetic_runner import SyntheticRunResult, run_a0_reference_world
from .synthetic_study import SyntheticWorldSpec


class QueueReferenceAdapter:
    """Compatibility adapter for the verified queue reference experiment.

    This is deliberately provisional and remains under the organizational layer until
    the three-domain promotion gate in TRD-0001 is satisfied.
    """

    @cached_property
    def identity(self) -> DomainReferenceIdentity:
        return DomainReferenceIdentity(
            domain_id="synthetic-queue-reference",
            reference_version="v1",
            process_reference="provisional-organizational-reference",
            specification_path="docs/organizational/evidence/synthetic-reference-v1/RESULT.md",
        )

    def parameter_space(self) -> ParameterSpaceSpec:
        design = build_reference_synthetic_experiment_v1()
        baseline = design.baseline.parameters
        return ParameterSpaceSpec(
            parameters={
                name: ParameterDefinition(
                    name=name,
                    low=bounds.low,
                    high=bounds.high,
                    default=float(baseline[name]),
                )
                for name, bounds in design.protocol.parameter_ranges.items()
            }
        )

    def agency_capabilities(self) -> tuple[AgencyCapabilitySpec, ...]:
        return (
            AgencyCapabilitySpec(level=AgencyLevel.A0),
            AgencyCapabilitySpec(
                level=AgencyLevel.A1,
                configurations=(
                    AgencyConfigurationSpec(
                        configuration_id="batching",
                        parameters={
                            "backlog_trigger": AgencyParameterSpec(
                                name="backlog_trigger",
                                minimum=0.0,
                                default=2.0,
                            ),
                            "service_multiplier": AgencyParameterSpec(
                                name="service_multiplier",
                                minimum=0.0,
                                maximum=1.0,
                                default=0.70,
                            ),
                            "adaptation_time": AgencyParameterSpec(
                                name="adaptation_time",
                                minimum=0.0,
                                default=0.05,
                            ),
                            "adaptation_cost_rate": AgencyParameterSpec(
                                name="adaptation_cost_rate",
                                minimum=0.0,
                                default=0.15,
                            ),
                        },
                    ),
                ),
            ),
        )

    def classify_regime(
        self,
        world: SyntheticWorldSpec,
    ) -> AnalyticalWorldReference | object:
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
            regime = GroundTruthClaim(
                claim_id="queue.regime",
                kind=GroundTruthKind.MECHANISTIC,
                target_kind=GroundTruthTargetKind.REGIME,
                target="stability",
                expected="stable" if reference.stable else "saturated",
                eligible=True,
                assumptions=(
                    "Regime is derived from configured arrival/service/rework mechanics only.",
                ),
                provenance=(
                    "src/sose/organizational/synthetic_analysis.py:analytical_world_reference",
                ),
            )
            lead = GroundTruthClaim(
                claim_id="queue.mean_lead_time",
                kind=GroundTruthKind.ANALYTICAL,
                target_kind=GroundTruthTargetKind.METRIC,
                target="mean_lead_time",
                expected=reference.expected_mean_lead_time,
                eligible=reference.expected_mean_lead_time is not None,
                assumptions=(
                    "Stationary M/G/1 mean lead time is valid only when offered load is below one.",
                    "Service and rework moments are implied by the configured synthetic queue mechanics.",
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
                    target="a1_stability",
                    expected=reference.kind.value,
                    eligible=True,
                    assumptions=(
                        "A1 regime is derived from configured nominal load, adaptation multiplier, and adaptation time.",
                        "No realized WIP, lead time, throughput, or backlog metric defines the regime label.",
                    ),
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

from __future__ import annotations

from enum import StrEnum
from functools import lru_cache
from statistics import fmean

from pydantic import BaseModel, ConfigDict, Field, InstanceOf

from .agency import A1Policy, A1PolicyName, AgencyLevel, AgencySpec
from .synthetic_a1 import (
    A1AdaptivePolicyParameters,
    A1AdaptiveRunResult,
    run_a1_reference_world,
)
from .synthetic_analysis import analytical_world_reference
from .synthetic_reference_experiment import (
    REFERENCE_DESIGN_SEED_V1,
    REFERENCE_ROOT_SEED_V1,
    ReferenceSyntheticExperimentDesignV1,
    build_reference_synthetic_experiment_v1,
)
from .synthetic_runner import SyntheticRunResult, run_a0_reference_world
from .synthetic_study import SyntheticWorldSpec, generate_synthetic_worlds


class A1RegimeKind(StrEnum):
    NOMINAL_STABLE = "nominal_stable"
    ADAPTATION_STABILIZED = "adaptation_stabilized"
    SATURATED = "saturated"


class A1RegimeReference(BaseModel):
    """Mechanistic A1 stability reference from workload drift above/below the trigger."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    world_hash: str
    nominal_load: float = Field(gt=0.0, allow_inf_nan=False)
    adapted_load: float = Field(gt=0.0, allow_inf_nan=False)
    kind: A1RegimeKind

    @property
    def stable(self) -> bool:
        return self.kind is not A1RegimeKind.SATURATED


class A0A1RunPair(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, arbitrary_types_allowed=True)

    design_index: int
    arm_id: str
    replication: int
    a0_run: InstanceOf[SyntheticRunResult]
    a1_run: InstanceOf[A1AdaptiveRunResult]
    a0_stable: bool
    a1_regime: A1RegimeKind
    delta_mean_lead_time: float
    delta_throughput: float
    delta_operating_cost: float


class A0A1ExperimentSummary(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    a0_stable_worlds: int = Field(ge=0)
    a0_saturated_worlds: int = Field(ge=0)
    a1_nominal_stable_worlds: int = Field(ge=0)
    a1_adaptation_stabilized_worlds: int = Field(ge=0)
    a1_saturated_worlds: int = Field(ge=0)
    saturated_to_stable_worlds: int = Field(ge=0)
    stable_to_saturated_worlds: int = Field(ge=0)
    mean_delta_lead_time: float = Field(allow_inf_nan=False)
    mean_delta_throughput: float = Field(allow_inf_nan=False)
    mean_delta_operating_cost: float = Field(allow_inf_nan=False)
    mean_adaptation_actor_time: float = Field(ge=0.0, allow_inf_nan=False)
    mean_adaptation_cost: float = Field(ge=0.0, allow_inf_nan=False)


class A0A1ReferenceExperimentResultV1(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, arbitrary_types_allowed=True)

    design: InstanceOf[ReferenceSyntheticExperimentDesignV1]
    pairs: tuple[InstanceOf[A0A1RunPair], ...]
    summary: InstanceOf[A0A1ExperimentSummary]


def build_a0_a1_reference_design_v1() -> ReferenceSyntheticExperimentDesignV1:
    """Reuse the v1 exogenous DOE while introducing explicit A1 local adaptation."""

    base = build_reference_synthetic_experiment_v1()
    protocol = base.protocol.model_copy(
        update={"agency_levels": (AgencyLevel.A0, AgencyLevel.A1)}
    )
    a1_policy = A1Policy(
        name=A1PolicyName.BATCHING,
        parameters={
            "backlog_trigger": 2.0,
            "service_multiplier": 0.70,
            "adaptation_time": 0.05,
            "adaptation_cost_rate": 0.15,
        },
    )
    worlds = generate_synthetic_worlds(
        protocol=protocol,
        baseline=base.baseline,
        interventions=base.interventions,
        agency_specs={
            AgencyLevel.A0: AgencySpec(level=AgencyLevel.A0),
            AgencyLevel.A1: AgencySpec(level=AgencyLevel.A1, a1_policy=a1_policy),
        },
        seed=REFERENCE_DESIGN_SEED_V1,
    )
    paired_crn_worlds = tuple(
        world.model_copy(
            update={"crn_group": f"design:{world.design_index}:arm:{world.arm_id}"}
        )
        for world in worlds
    )
    return ReferenceSyntheticExperimentDesignV1(
        design_seed=REFERENCE_DESIGN_SEED_V1,
        root_seed=REFERENCE_ROOT_SEED_V1,
        baseline=base.baseline,
        protocol=protocol,
        interventions=base.interventions,
        worlds=paired_crn_worlds,
    )


def classify_a1_regime(world: SyntheticWorldSpec) -> A1RegimeReference:
    """Classify A1 stability from exogenous mechanics plus the fixed local policy.

    Below the backlog trigger, nominal workload drift applies. Above it, every
    serviced item pays adaptation time and receives the configured service multiplier.
    If adapted drift is negative, threshold control can stabilize a nominally
    overloaded world; if it remains non-negative, no local adaptation can drain it.
    """

    if world.agency_level is not AgencyLevel.A1:
        raise ValueError("A1 regime classification requires an A1 world")
    agency = world.model_spec.agency
    if agency.a1_policy is None:
        raise ValueError("A1 world requires an adaptive policy")
    policy = A1AdaptivePolicyParameters.from_policy(agency.a1_policy)

    parameters = world.model_spec.parameters
    arrival_rate = float(parameters["arrival_rate"])
    capacity = float(parameters["service_capacity"])
    rework_probability = float(parameters["rework_probability"])
    if arrival_rate <= 0.0 or capacity <= 0.0:
        raise ValueError("arrival_rate and service_capacity must be positive")
    if not 0.0 <= rework_probability <= 1.0:
        raise ValueError("rework_probability must be between zero and one")

    nominal_service = (1.0 + rework_probability) / capacity
    adapted_service = nominal_service * policy.service_multiplier + policy.adaptation_time
    nominal_load = arrival_rate * nominal_service
    adapted_load = arrival_rate * adapted_service

    if nominal_load < 1.0:
        kind = A1RegimeKind.NOMINAL_STABLE
    elif adapted_load < 1.0:
        kind = A1RegimeKind.ADAPTATION_STABILIZED
    else:
        kind = A1RegimeKind.SATURATED

    return A1RegimeReference(
        world_hash=world.world_hash,
        nominal_load=nominal_load,
        adapted_load=adapted_load,
        kind=kind,
    )


@lru_cache(maxsize=1)
def run_a0_a1_reference_experiment_v1() -> A0A1ReferenceExperimentResultV1:
    """Run the exact preregistered paired A0/A1 reference experiment."""

    design = build_a0_a1_reference_design_v1()
    replication_plan = design.protocol.replication_plan
    if replication_plan.min_replications != replication_plan.max_replications:
        raise ValueError("reference v1 requires a fixed replication count")
    replications = replication_plan.min_replications
    by_key: dict[tuple[int, str], dict[AgencyLevel, SyntheticWorldSpec]] = {}
    for world in design.worlds:
        by_key.setdefault((world.design_index, world.arm_id), {})[
            world.agency_level
        ] = world

    pairs: list[A0A1RunPair] = []
    regime_by_key: dict[tuple[int, str], A1RegimeReference] = {}
    a0_stable_by_key: dict[tuple[int, str], bool] = {}

    for key in sorted(by_key):
        worlds = by_key[key]
        if set(worlds) != {AgencyLevel.A0, AgencyLevel.A1}:
            raise ValueError("each design/arm requires exactly one A0 and one A1 world")
        a0_world = worlds[AgencyLevel.A0]
        a1_world = worlds[AgencyLevel.A1]
        if a0_world.exogenous_parameters != a1_world.exogenous_parameters:
            raise ValueError("A0/A1 pairs must share identical exogenous parameters")
        if a0_world.crn_group != a1_world.crn_group:
            raise ValueError("A0/A1 pairs must share one CRN group")

        a0_stable = analytical_world_reference(a0_world).stable
        a1_regime = classify_a1_regime(a1_world)
        a0_stable_by_key[key] = a0_stable
        regime_by_key[key] = a1_regime

        for replication in range(replications):
            a0_run = run_a0_reference_world(
                protocol=design.protocol,
                world=a0_world,
                root_seed=design.root_seed,
                replication=replication,
            )
            a1_run = run_a1_reference_world(
                protocol=design.protocol,
                world=a1_world,
                root_seed=design.root_seed,
                replication=replication,
            )
            pairs.append(
                A0A1RunPair(
                    design_index=key[0],
                    arm_id=key[1],
                    replication=replication,
                    a0_run=a0_run,
                    a1_run=a1_run,
                    a0_stable=a0_stable,
                    a1_regime=a1_regime.kind,
                    delta_mean_lead_time=a1_run.mean_lead_time - a0_run.mean_lead_time,
                    delta_throughput=a1_run.throughput - a0_run.throughput,
                    delta_operating_cost=(
                        a1_run.total_operating_cost - a0_run.total_operating_cost
                    ),
                )
            )

    references = tuple(regime_by_key.values())
    a0_stable_count = sum(a0_stable_by_key.values())
    a0_saturated_count = len(a0_stable_by_key) - a0_stable_count
    rescued = sum(
        (not a0_stable_by_key[key]) and regime_by_key[key].stable
        for key in regime_by_key
    )
    worsened = sum(
        a0_stable_by_key[key] and not regime_by_key[key].stable
        for key in regime_by_key
    )

    return A0A1ReferenceExperimentResultV1(
        design=design,
        pairs=tuple(pairs),
        summary=A0A1ExperimentSummary(
            a0_stable_worlds=a0_stable_count,
            a0_saturated_worlds=a0_saturated_count,
            a1_nominal_stable_worlds=sum(
                ref.kind is A1RegimeKind.NOMINAL_STABLE for ref in references
            ),
            a1_adaptation_stabilized_worlds=sum(
                ref.kind is A1RegimeKind.ADAPTATION_STABILIZED for ref in references
            ),
            a1_saturated_worlds=sum(
                ref.kind is A1RegimeKind.SATURATED for ref in references
            ),
            saturated_to_stable_worlds=rescued,
            stable_to_saturated_worlds=worsened,
            mean_delta_lead_time=fmean(pair.delta_mean_lead_time for pair in pairs),
            mean_delta_throughput=fmean(pair.delta_throughput for pair in pairs),
            mean_delta_operating_cost=fmean(
                pair.delta_operating_cost for pair in pairs
            ),
            mean_adaptation_actor_time=fmean(
                pair.a1_run.adaptation_actor_time_measurement for pair in pairs
            ),
            mean_adaptation_cost=fmean(
                pair.a1_run.adaptation_cost for pair in pairs
            ),
        ),
    )

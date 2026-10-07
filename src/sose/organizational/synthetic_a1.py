from __future__ import annotations

from math import ceil, cos, exp, log, pi, sqrt
from statistics import fmean, median

from pydantic import BaseModel, ConfigDict, Field, model_validator

from sose.core.randomness import CounterRandomSource, scoped_seed

from .agency import A1Policy, A1PolicyName, AgencyLevel
from .experiment import ExperimentProtocol
from .ledger import ActorCategory, ActorLedger, ActorLedgerInterval
from .synthetic_runner import SyntheticItemRecord
from .synthetic_study import SyntheticWorldSpec


class A1AdaptivePolicyParameters(BaseModel):
    """Typed semantics for the A1 batching policy used by the reference process."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    backlog_trigger: float = Field(ge=0.0, allow_inf_nan=False)
    service_multiplier: float = Field(gt=0.0, le=1.0, allow_inf_nan=False)
    adaptation_time: float = Field(ge=0.0, allow_inf_nan=False)
    adaptation_cost_rate: float = Field(default=0.0, ge=0.0, allow_inf_nan=False)

    @classmethod
    def from_policy(cls, policy: A1Policy) -> "A1AdaptivePolicyParameters":
        if policy.name is not A1PolicyName.BATCHING:
            raise ValueError("A1 reference runner currently implements batching only")
        return cls.model_validate(dict(policy.parameters))


class A1AdaptiveRunResult(BaseModel):
    """One deterministic A1 execution with explicit adaptation accounting."""

    model_config = ConfigDict(extra="forbid", frozen=True, arbitrary_types_allowed=True)

    world_hash: str
    model_spec_hash: str
    protocol_hash: str
    design_index: int = Field(ge=0)
    arm_id: str
    replication: int = Field(ge=0)
    replication_seed: int = Field(ge=0)
    arrived_items: int = Field(ge=0)
    completed_items: int = Field(ge=0)
    throughput: float = Field(ge=0.0, allow_inf_nan=False)
    mean_wip: float = Field(ge=0.0, allow_inf_nan=False)
    mean_lead_time: float = Field(ge=0.0, allow_inf_nan=False)
    median_lead_time: float = Field(ge=0.0, allow_inf_nan=False)
    p90_lead_time: float = Field(ge=0.0, allow_inf_nan=False)
    rework_fraction: float = Field(ge=0.0, le=1.0, allow_inf_nan=False)
    total_operating_cost: float = Field(ge=0.0, allow_inf_nan=False)
    adaptation_count: int = Field(ge=0)
    adaptation_actor_time: float = Field(ge=0.0, allow_inf_nan=False)
    adaptation_actor_time_measurement: float = Field(ge=0.0, allow_inf_nan=False)
    adaptation_cost: float = Field(ge=0.0, allow_inf_nan=False)
    actor_observed_until: float = Field(ge=0.0, allow_inf_nan=False)
    actor_ledger: ActorLedger
    items: tuple[SyntheticItemRecord, ...]

    @model_validator(mode="after")
    def bind_adaptation_ledger(self) -> "A1AdaptiveRunResult":
        recorded = self.actor_ledger.duration_by_category().get(
            ActorCategory.ADAPTATION,
            0.0,
        )
        if abs(recorded - self.adaptation_actor_time) > 1e-12:
            raise ValueError("adaptation actor time must equal ADAPTATION ledger time")
        return self


def run_a1_reference_world(
    *,
    protocol: ExperimentProtocol,
    world: SyntheticWorldSpec,
    root_seed: int,
    replication: int,
) -> A1AdaptiveRunResult:
    """Run one A1 world with local backlog-triggered batching adaptation.

    The actor may adapt its local execution policy, but the ModelSpec structure,
    nominal capacity, routing, demand process, and intervention remain unchanged.
    """

    if world.protocol_hash != protocol.protocol_hash:
        raise ValueError("synthetic world protocol hash mismatch")
    if world.agency_level is not AgencyLevel.A1:
        raise ValueError("A1 reference runner requires an A1 world")
    agency = world.model_spec.agency
    if agency.level is not AgencyLevel.A1 or agency.a1_policy is None:
        raise ValueError("A1 world must carry an A1 policy")
    policy = A1AdaptivePolicyParameters.from_policy(agency.a1_policy)

    parameters = world.model_spec.parameters
    arrival_rate = _positive(parameters, "arrival_rate")
    service_capacity = _positive(parameters, "service_capacity")
    service_cv = _nonnegative(parameters, "service_cv")
    rework_probability = _probability(parameters, "rework_probability")

    replication_seed = scoped_seed(root_seed, world.crn_group, replication)
    rng = CounterRandomSource(replication_seed)

    arrival_at = 0.0
    server_available_at = world.intervention_transition_time
    items: list[SyntheticItemRecord] = []
    actor_intervals: list[ActorLedgerInterval] = []
    actor_cursor = 0.0
    adaptation_count = 0
    adaptation_actor_time = 0.0
    item_index = 0

    while True:
        latent = rng.uniform(
            stream="synthetic_arrivals",
            entity_id=item_index,
            mechanism="interarrival",
        )
        arrival_at += -log(max(1.0 - latent, 1e-15)) / arrival_rate
        if arrival_at > protocol.horizon:
            break

        service_z = _standard_normal(
            rng,
            stream="synthetic_service",
            entity_id=item_index,
            mechanism="primary",
        )
        primary = _lognormal_with_mean_cv(
            mean=1.0 / service_capacity,
            cv=service_cv,
            standard_normal=service_z,
        )
        rework_latent = rng.uniform(
            stream="synthetic_rework",
            entity_id=item_index,
            mechanism="occurs",
        )
        reworked = rework_latent < rework_probability
        rework = 0.0
        if reworked:
            rework_z = _standard_normal(
                rng,
                stream="synthetic_service",
                entity_id=item_index,
                mechanism="rework",
            )
            rework = _lognormal_with_mean_cv(
                mean=1.0 / service_capacity,
                cv=service_cv,
                standard_normal=rework_z,
            )

        backlog_pressure = max(0.0, server_available_at - arrival_at)
        adapting = backlog_pressure >= policy.backlog_trigger
        service_multiplier = policy.service_multiplier if adapting else 1.0
        processing_time = primary * service_multiplier
        rework_time = rework * service_multiplier

        capacity_start = max(arrival_at, server_available_at)
        if actor_cursor < capacity_start:
            actor_intervals.append(
                ActorLedgerInterval(
                    start=actor_cursor,
                    end=capacity_start,
                    category=ActorCategory.IDLE,
                    model_spec_hash=world.model_spec_hash,
                )
            )
            actor_cursor = capacity_start

        if adapting and policy.adaptation_time > 0.0:
            adaptation_end = actor_cursor + policy.adaptation_time
            actor_intervals.append(
                ActorLedgerInterval(
                    start=actor_cursor,
                    end=adaptation_end,
                    category=ActorCategory.ADAPTATION,
                    work_item_id=f"item-{item_index}",
                    secondary_labels=("a1_batching",),
                    model_spec_hash=world.model_spec_hash,
                )
            )
            actor_cursor = adaptation_end
            adaptation_actor_time += policy.adaptation_time
        if adapting:
            adaptation_count += 1

        service_start = actor_cursor
        execution_time = processing_time + rework_time
        completed_at = actor_cursor + execution_time
        if execution_time > 0.0:
            actor_intervals.append(
                ActorLedgerInterval(
                    start=actor_cursor,
                    end=completed_at,
                    category=ActorCategory.EXECUTION,
                    work_item_id=f"item-{item_index}",
                    secondary_labels=("adapted",) if adapting else (),
                    model_spec_hash=world.model_spec_hash,
                )
            )
        actor_cursor = completed_at
        server_available_at = completed_at

        queue_time = service_start - arrival_at
        lead_time = completed_at - arrival_at
        items.append(
            SyntheticItemRecord(
                item_id=f"item-{item_index}",
                arrival_at=arrival_at,
                service_start=service_start,
                completed_at=completed_at,
                queue_time=queue_time,
                processing_time=processing_time,
                rework_time=rework_time,
                lead_time=lead_time,
                reworked=reworked,
                service_latent_normal=service_z,
                rework_latent_uniform=rework_latent,
            )
        )
        item_index += 1

    actor_observed_until = max(protocol.horizon, actor_cursor)
    if actor_cursor < actor_observed_until:
        actor_intervals.append(
            ActorLedgerInterval(
                start=actor_cursor,
                end=actor_observed_until,
                category=ActorCategory.IDLE,
                model_spec_hash=world.model_spec_hash,
            )
        )

    actor_ledger = ActorLedger(
        actor_id="synthetic-actor-0",
        intervals=tuple(actor_intervals),
    )
    actor_ledger.assert_complete(start=0.0, end=actor_observed_until)

    measurement_start = protocol.warmup
    measurement_end = protocol.horizon
    measurement_duration = measurement_end - measurement_start
    lead_time_cohort = tuple(
        item
        for item in items
        if item.arrival_at >= measurement_start and item.completed_at <= measurement_end
    )
    measurement_departures = tuple(
        item
        for item in items
        if measurement_start < item.completed_at <= measurement_end
    )
    leads = tuple(item.lead_time for item in lead_time_cohort)
    throughput = len(measurement_departures) / measurement_duration
    mean_wip = sum(
        max(
            0.0,
            min(item.completed_at, measurement_end)
            - max(item.arrival_at, measurement_start),
        )
        for item in items
    ) / measurement_duration

    if leads:
        ordered = tuple(sorted(leads))
        mean_lead = fmean(ordered)
        median_lead = median(ordered)
        p90_lead = ordered[max(1, ceil(0.90 * len(ordered))) - 1]
        rework_fraction = (
            sum(item.reworked for item in lead_time_cohort) / len(lead_time_cohort)
        )
    else:
        mean_lead = median_lead = p90_lead = rework_fraction = 0.0

    adaptation_actor_time_measurement = sum(
        max(
            0.0,
            min(interval.end, measurement_end)
            - max(interval.start, measurement_start),
        )
        for interval in actor_intervals
        if interval.category is ActorCategory.ADAPTATION
    )
    adaptation_cost = (
        adaptation_actor_time_measurement * policy.adaptation_cost_rate
    )
    total_operating_cost = (
        (_cost_rate(world) + world.intervention_operating_cost) * measurement_duration
        + world.intervention_transition_cost
        + _uncertain_transition_cost(protocol, world)
        + adaptation_cost
    )

    return A1AdaptiveRunResult(
        world_hash=world.world_hash,
        model_spec_hash=world.model_spec_hash,
        protocol_hash=protocol.protocol_hash,
        design_index=world.design_index,
        arm_id=world.arm_id,
        replication=replication,
        replication_seed=replication_seed,
        arrived_items=len(items),
        completed_items=sum(item.completed_at <= measurement_end for item in items),
        throughput=throughput,
        mean_wip=mean_wip,
        mean_lead_time=mean_lead,
        median_lead_time=median_lead,
        p90_lead_time=p90_lead,
        rework_fraction=rework_fraction,
        total_operating_cost=total_operating_cost,
        adaptation_count=adaptation_count,
        adaptation_actor_time=adaptation_actor_time,
        adaptation_actor_time_measurement=adaptation_actor_time_measurement,
        adaptation_cost=adaptation_cost,
        actor_observed_until=actor_observed_until,
        actor_ledger=actor_ledger,
        items=tuple(items),
    )


def _standard_normal(
    rng: CounterRandomSource,
    *,
    stream: str,
    entity_id: object,
    mechanism: str,
) -> float:
    u1 = max(
        rng.uniform(
            stream=stream,
            entity_id=entity_id,
            mechanism=f"{mechanism}:u1",
        ),
        1e-15,
    )
    u2 = rng.uniform(
        stream=stream,
        entity_id=entity_id,
        mechanism=f"{mechanism}:u2",
    )
    return sqrt(-2.0 * log(u1)) * cos(2.0 * pi * u2)


def _lognormal_with_mean_cv(
    *,
    mean: float,
    cv: float,
    standard_normal: float,
) -> float:
    if cv == 0.0:
        return mean
    sigma2 = log(1.0 + cv * cv)
    sigma = sqrt(sigma2)
    mu = log(mean) - sigma2 / 2.0
    return exp(mu + sigma * standard_normal)


def _positive(parameters: object, name: str) -> float:
    value = float(parameters[name])
    if value <= 0.0:
        raise ValueError(f"{name} must be positive")
    return value


def _nonnegative(parameters: object, name: str) -> float:
    value = float(parameters[name])
    if value < 0.0:
        raise ValueError(f"{name} must be non-negative")
    return value


def _probability(parameters: object, name: str) -> float:
    value = float(parameters[name])
    if not 0.0 <= value <= 1.0:
        raise ValueError(f"{name} must be between zero and one")
    return value


def _cost_rate(world: SyntheticWorldSpec) -> float:
    value = float(world.model_spec.costs.get("operating_cost", 0.0))
    if value < 0.0:
        raise ValueError("operating_cost must be non-negative")
    return value


def _uncertain_transition_cost(
    protocol: ExperimentProtocol,
    world: SyntheticWorldSpec,
) -> float:
    if world.arm_id == "baseline" or protocol.cost_analysis is None:
        return 0.0
    parameter = protocol.cost_analysis.transition_cost_parameter
    return float(world.exogenous_parameters.get(parameter, 0.0))

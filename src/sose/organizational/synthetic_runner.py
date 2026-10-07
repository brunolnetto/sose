from __future__ import annotations

from hashlib import sha256
import json
from math import ceil, cos, exp, log, pi, sqrt
from statistics import fmean, median
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from sose.core.randomness import CounterRandomSource, scoped_seed

from .experiment import ExperimentProtocol
from .synthetic_study import SyntheticWorldSpec


NonBlankString = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]


class SyntheticItemRecord(BaseModel):
    """Item-level synthetic truth emitted by the reference organizational process."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    item_id: NonBlankString
    arrival_at: float = Field(ge=0.0, allow_inf_nan=False)
    service_start: float = Field(ge=0.0, allow_inf_nan=False)
    completed_at: float = Field(ge=0.0, allow_inf_nan=False)
    queue_time: float = Field(ge=0.0, allow_inf_nan=False)
    processing_time: float = Field(ge=0.0, allow_inf_nan=False)
    rework_time: float = Field(ge=0.0, allow_inf_nan=False)
    lead_time: float = Field(ge=0.0, allow_inf_nan=False)
    reworked: bool
    service_latent_normal: float = Field(allow_inf_nan=False)
    rework_latent_uniform: float = Field(ge=0.0, lt=1.0, allow_inf_nan=False)


class SyntheticRunResult(BaseModel):
    """One replicated execution result for a single synthetic world."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    world_hash: NonBlankString
    protocol_hash: NonBlankString
    design_index: int = Field(ge=0)
    arm_id: NonBlankString
    agency_level: NonBlankString
    crn_group: NonBlankString
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
    items: tuple[SyntheticItemRecord, ...]


class SyntheticExperimentDataset(BaseModel):
    """Hash-addressed synthetic dataset; no empirical observations are consumed."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    protocol_hash: NonBlankString
    root_seed: int = Field(ge=0)
    replications: int = Field(ge=2)
    runs: tuple[SyntheticRunResult, ...] = Field(min_length=1)

    def canonical_payload(self) -> dict[str, object]:
        return {
            "protocol_hash": self.protocol_hash,
            "root_seed": self.root_seed,
            "replications": self.replications,
            "runs": [run.model_dump(mode="json") for run in self.runs],
        }

    @property
    def dataset_hash(self) -> str:
        payload = json.dumps(
            self.canonical_payload(),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
        return sha256(payload).hexdigest()


def run_reference_synthetic_experiment(
    *,
    protocol: ExperimentProtocol,
    worlds: tuple[SyntheticWorldSpec, ...],
    root_seed: int,
    replications: int | None = None,
) -> SyntheticExperimentDataset:
    """Run a controlled open-system FCFS reference process over synthetic worlds.

    The reference process is intentionally explicit: Poisson arrivals, one finite
    service resource, lognormal service demand with configured CV, and optional
    same-resource rework. It is a known data-generating mechanism for recovery
    experiments, not an empirical claim about all organizations.
    """

    count = (
        protocol.replication_plan.min_replications
        if replications is None
        else replications
    )
    if not (
        protocol.replication_plan.min_replications
        <= count
        <= protocol.replication_plan.max_replications
    ):
        raise ValueError("replications must remain inside the preregistered replication bounds")

    expected_worlds = (
        protocol.sample_size
        * (1 + len(protocol.intervention_ids))
        * len(protocol.agency_levels)
    )
    identities = {
        (world.design_index, world.arm_id, world.agency_level.value)
        for world in worlds
    }
    if len(worlds) != expected_worlds or len(identities) != expected_worlds:
        raise ValueError("complete synthetic world design is required")
    if any(world.protocol_hash != protocol.protocol_hash for world in worlds):
        raise ValueError("synthetic world protocol hash mismatch")
    if any(world.agency_level.value != "A0" for world in worlds):
        raise ValueError(
            "reference synthetic runner currently implements A0 mechanics only"
        )

    runs: list[SyntheticRunResult] = []
    for world in worlds:
        for replication in range(count):
            runs.append(
                _run_reference_world(
                    protocol=protocol,
                    world=world,
                    root_seed=root_seed,
                    replication=replication,
                )
            )

    return SyntheticExperimentDataset(
        protocol_hash=protocol.protocol_hash,
        root_seed=root_seed,
        replications=count,
        runs=tuple(runs),
    )


def _run_reference_world(
    *,
    protocol: ExperimentProtocol,
    world: SyntheticWorldSpec,
    root_seed: int,
    replication: int,
) -> SyntheticRunResult:
    parameters = world.model_spec.parameters
    arrival_rate = _positive_parameter(parameters, "arrival_rate")
    service_capacity = _positive_parameter(parameters, "service_capacity")
    service_cv = _nonnegative_parameter(parameters, "service_cv")
    rework_probability = _probability_parameter(parameters, "rework_probability")

    replication_seed = scoped_seed(root_seed, world.crn_group, replication)
    rng = CounterRandomSource(replication_seed)

    arrival_at = 0.0
    server_available_at = world.intervention_transition_time
    items: list[SyntheticItemRecord] = []
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
        processing_time = _lognormal_with_mean_cv(
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
        rework_time = 0.0
        if reworked:
            rework_z = _standard_normal(
                rng,
                stream="synthetic_service",
                entity_id=item_index,
                mechanism="rework",
            )
            rework_time = _lognormal_with_mean_cv(
                mean=1.0 / service_capacity,
                cv=service_cv,
                standard_normal=rework_z,
            )

        service_start = max(arrival_at, server_available_at)
        queue_time = service_start - arrival_at
        completed_at = service_start + processing_time + rework_time
        server_available_at = completed_at
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

    measurement_start = protocol.warmup
    measurement_end = protocol.horizon
    measurement_duration = measurement_end - measurement_start
    completed = tuple(
        item
        for item in items
        if item.arrival_at >= measurement_start and item.completed_at <= measurement_end
    )
    leads = tuple(item.lead_time for item in completed)
    completed_items = sum(item.completed_at <= measurement_end for item in items)
    throughput = len(completed) / measurement_duration
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
        rework_fraction = sum(item.reworked for item in completed) / len(completed)
    else:
        mean_lead = median_lead = p90_lead = rework_fraction = 0.0

    base_cost_rate = _cost_rate(world)
    uncertain_transition_cost = (
        float(world.exogenous_parameters.get("transition_cost", 0.0))
        if world.arm_id != "baseline"
        else 0.0
    )
    total_cost = (
        (base_cost_rate + world.intervention_operating_cost) * measurement_duration
        + world.intervention_transition_cost
        + uncertain_transition_cost
    )

    return SyntheticRunResult(
        world_hash=world.world_hash,
        protocol_hash=protocol.protocol_hash,
        design_index=world.design_index,
        arm_id=world.arm_id,
        agency_level=world.agency_level.value,
        crn_group=world.crn_group,
        replication=replication,
        replication_seed=replication_seed,
        arrived_items=len(items),
        completed_items=completed_items,
        throughput=throughput,
        mean_wip=mean_wip,
        mean_lead_time=mean_lead,
        median_lead_time=median_lead,
        p90_lead_time=p90_lead,
        rework_fraction=rework_fraction,
        total_operating_cost=total_cost,
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


def _positive_parameter(parameters: object, name: str) -> float:
    value = float(parameters[name])
    if value <= 0.0:
        raise ValueError(f"{name} must be positive")
    return value


def _nonnegative_parameter(parameters: object, name: str) -> float:
    value = float(parameters[name])
    if value < 0.0:
        raise ValueError(f"{name} must be non-negative")
    return value


def _probability_parameter(parameters: object, name: str) -> float:
    value = float(parameters[name])
    if not 0.0 <= value <= 1.0:
        raise ValueError(f"{name} must be between zero and one")
    return value


def _cost_rate(world: SyntheticWorldSpec) -> float:
    raw = world.model_spec.costs.get("operating_cost", 0.0)
    value = float(raw)
    if value < 0.0:
        raise ValueError("operating_cost must be non-negative")
    return value

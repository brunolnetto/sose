from __future__ import annotations

from hashlib import sha256
import json
from math import log
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from sose.core.randomness import CounterRandomSource, scoped_seed

from .experiment import ExperimentProtocol
from .synthetic_mechanics import (
    _draw_service,
    _lead_statistics,
    _lognormal_with_mean_cv,  # noqa: F401 - historical import location
    _standard_normal,  # noqa: F401 - historical import location
    _throughput_and_wip,
)
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

    count = _replication_count(protocol, replications)
    _require_complete_a0_design(protocol, worlds)

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


def run_a0_reference_world(
    *,
    protocol: ExperimentProtocol,
    world: SyntheticWorldSpec,
    root_seed: int,
    replication: int,
) -> SyntheticRunResult:
    """Run one A0 reference world while preserving the public CRN contract."""

    if world.protocol_hash != protocol.protocol_hash:
        raise ValueError("synthetic world protocol hash mismatch")
    if world.agency_level.value != "A0":
        raise ValueError("A0 reference runner requires an A0 world")
    return _run_reference_world(
        protocol=protocol,
        world=world,
        root_seed=root_seed,
        replication=replication,
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

        draw = _draw_service(
            rng, item_index,
            service_capacity=service_capacity,
            service_cv=service_cv,
            rework_probability=rework_probability,
        )
        processing_time = draw.primary
        rework_time = draw.rework

        service_start = max(arrival_at, server_available_at)
        queue_time = service_start - arrival_at
        completed_at = service_start + processing_time + rework_time
        server_available_at = completed_at
        lead_time = queue_time + processing_time + rework_time

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
                reworked=draw.reworked,
                service_latent_normal=draw.service_z,
                rework_latent_uniform=draw.rework_latent,
            )
        )
        item_index += 1

    measurement_start = protocol.warmup
    measurement_end = protocol.horizon
    measurement_duration = measurement_end - measurement_start
    completed_items = sum(item.completed_at <= measurement_end for item in items)
    throughput, mean_wip = _throughput_and_wip(items, measurement_start, measurement_end)
    mean_lead, median_lead, p90_lead, rework_fraction = _lead_statistics(
        items, measurement_start, measurement_end,
    )
    total_cost = _a0_total_cost(protocol, world, measurement_duration)

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


def _replication_count(protocol: ExperimentProtocol, replications: int | None) -> int:
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
    return count


def _require_complete_a0_design(
    protocol: ExperimentProtocol, worlds: tuple[SyntheticWorldSpec, ...],
) -> None:
    if any(world.agency_level.value != "A0" for world in worlds):
        raise ValueError(
            "reference synthetic runner currently implements A0 mechanics only"
        )
    if not _design_complete(protocol, worlds):
        raise ValueError("complete synthetic world design is required")
    if any(world.protocol_hash != protocol.protocol_hash for world in worlds):
        raise ValueError("synthetic world protocol hash mismatch")


def _design_complete(
    protocol: ExperimentProtocol, worlds: tuple[SyntheticWorldSpec, ...],
) -> bool:
    expected_identities = {
        (design_index, arm_id, level.value)
        for design_index in range(protocol.sample_size)
        for arm_id in ("baseline", *protocol.intervention_ids)
        for level in protocol.agency_levels
    }
    identities = {
        (world.design_index, world.arm_id, world.agency_level.value)
        for world in worlds
    }
    return identities == expected_identities and len(worlds) == len(expected_identities)


def _a0_total_cost(
    protocol: ExperimentProtocol, world: SyntheticWorldSpec, measurement_duration: float,
) -> float:
    base_cost_rate = _cost_rate(world)
    transition_parameter = (
        None
        if protocol.cost_analysis is None
        else protocol.cost_analysis.transition_cost_parameter
    )
    uncertain_transition_cost = (
        float(world.exogenous_parameters.get(transition_parameter, 0.0))
        if world.arm_id != "baseline" and transition_parameter is not None
        else 0.0
    )
    return (
        (base_cost_rate + world.intervention_operating_cost) * measurement_duration
        + world.intervention_transition_cost
        + uncertain_transition_cost
    )


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

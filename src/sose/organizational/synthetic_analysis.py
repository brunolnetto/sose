from __future__ import annotations

from hashlib import sha256
import json
from statistics import fmean
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from .experiment import ExperimentProtocol
from .synthetic_runner import SyntheticExperimentDataset
from .synthetic_study import SyntheticWorldSpec


NonBlankString = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]


class AnalyticalWorldReference(BaseModel):
    """Mechanistic ground truth implied by one A0 reference world."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    world_hash: NonBlankString
    design_index: int = Field(ge=0)
    arm_id: NonBlankString
    agency_level: NonBlankString
    arrival_rate: float = Field(gt=0.0, allow_inf_nan=False)
    expected_service_time: float = Field(gt=0.0, allow_inf_nan=False)
    service_second_moment: float = Field(gt=0.0, allow_inf_nan=False)
    offered_load: float = Field(gt=0.0, allow_inf_nan=False)
    stable: bool
    service_ceiling: float = Field(gt=0.0, allow_inf_nan=False)
    expected_throughput: float = Field(gt=0.0, allow_inf_nan=False)
    expected_mean_lead_time: float | None = Field(default=None, gt=0.0, allow_inf_nan=False)


class SyntheticWorldRecovery(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    world_hash: NonBlankString
    design_index: int = Field(ge=0)
    arm_id: NonBlankString
    agency_level: NonBlankString
    offered_load: float = Field(gt=0.0, allow_inf_nan=False)
    stable: bool
    expected_mean_lead_time: float | None = Field(default=None, gt=0.0, allow_inf_nan=False)
    observed_mean_lead_time: float = Field(ge=0.0, allow_inf_nan=False)
    observed_mean_throughput: float = Field(ge=0.0, allow_inf_nan=False)
    observed_mean_wip: float = Field(ge=0.0, allow_inf_nan=False)
    relative_error_mean_lead_time: float | None = Field(
        default=None,
        ge=0.0,
        allow_inf_nan=False,
    )


class SyntheticEffectRecovery(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    design_index: int = Field(ge=0)
    agency_level: NonBlankString
    arm_id: NonBlankString
    baseline_world_hash: NonBlankString
    intervention_world_hash: NonBlankString
    expected_delta_mean_lead_time: float | None = Field(default=None, allow_inf_nan=False)
    simulated_delta_mean_lead_time: float | None = Field(default=None, allow_inf_nan=False)
    null_region: bool
    direction_recovered: bool | None = None


class SyntheticRecoveryReport(BaseModel):
    """Verification report against known synthetic data-generating mechanics."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    protocol_hash: NonBlankString
    dataset_hash: NonBlankString
    world_count: int = Field(ge=1)
    stable_world_count: int = Field(ge=0)
    saturated_world_count: int = Field(ge=0)
    mean_relative_error_stable: float = Field(ge=0.0, allow_inf_nan=False)
    direction_recovery_rate: float = Field(ge=0.0, le=1.0, allow_inf_nan=False)
    worlds: tuple[SyntheticWorldRecovery, ...]
    effects: tuple[SyntheticEffectRecovery, ...]

    def canonical_payload(self) -> dict[str, object]:
        return self.model_dump(mode="json")

    @property
    def report_hash(self) -> str:
        payload = json.dumps(
            self.canonical_payload(),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
        return sha256(payload).hexdigest()


def analytical_world_reference(world: SyntheticWorldSpec) -> AnalyticalWorldReference:
    """Return M/G/1 ground truth implied by configured exogenous mechanics."""

    parameters = world.model_spec.parameters
    arrival_rate = float(parameters["arrival_rate"])
    service_capacity = float(parameters["service_capacity"])
    service_cv = float(parameters["service_cv"])
    rework_probability = float(parameters["rework_probability"])

    if arrival_rate <= 0.0 or service_capacity <= 0.0:
        raise ValueError("arrival_rate and service_capacity must be positive")
    if service_cv < 0.0:
        raise ValueError("service_cv must be non-negative")
    if not 0.0 <= rework_probability <= 1.0:
        raise ValueError("rework_probability must be between zero and one")

    primary_mean = 1.0 / service_capacity
    primary_second_moment = primary_mean * primary_mean * (1.0 + service_cv * service_cv)
    expected_service = primary_mean * (1.0 + rework_probability)
    service_second_moment = (
        primary_second_moment * (1.0 + rework_probability)
        + 2.0 * rework_probability * primary_mean * primary_mean
    )
    offered_load = arrival_rate * expected_service
    stable = offered_load < 1.0
    service_ceiling = 1.0 / expected_service

    if stable:
        expected_wait = (
            arrival_rate
            * service_second_moment
            / (2.0 * (1.0 - offered_load))
        )
        expected_mean_lead = expected_service + expected_wait
        expected_throughput = arrival_rate
    else:
        expected_mean_lead = None
        expected_throughput = service_ceiling

    return AnalyticalWorldReference(
        world_hash=world.world_hash,
        design_index=world.design_index,
        arm_id=world.arm_id,
        agency_level=world.agency_level.value,
        arrival_rate=arrival_rate,
        expected_service_time=expected_service,
        service_second_moment=service_second_moment,
        offered_load=offered_load,
        stable=stable,
        service_ceiling=service_ceiling,
        expected_throughput=expected_throughput,
        expected_mean_lead_time=expected_mean_lead,
    )


def analyze_reference_synthetic_experiment(
    *,
    protocol: ExperimentProtocol,
    worlds: tuple[SyntheticWorldSpec, ...],
    dataset: SyntheticExperimentDataset,
) -> SyntheticRecoveryReport:
    """Compare synthetic output with the known A0 queueing ground truth.

    Regime coordinates come only from configured exogenous parameters. Observed WIP,
    throughput, and delays remain outcomes and are never fed back into regime identity.
    """

    if dataset.protocol_hash != protocol.protocol_hash:
        raise ValueError("dataset protocol hash does not match experiment protocol")

    world_by_hash = {world.world_hash: world for world in worlds}
    run_world_hashes = {run.world_hash for run in dataset.runs}
    if len(world_by_hash) != len(worlds) or run_world_hashes != set(world_by_hash):
        raise ValueError("synthetic dataset/world binding is incomplete")
    if any(world.protocol_hash != protocol.protocol_hash for world in worlds):
        raise ValueError("synthetic world binding does not match experiment protocol")

    lead_outcome = next(
        (outcome for outcome in protocol.outcomes if outcome.name == "lead_time"),
        None,
    )
    if lead_outcome is None:
        raise ValueError("synthetic recovery requires a preregistered lead_time outcome")
    equivalence_margin = lead_outcome.equivalence_margin

    runs_by_world: dict[str, list[object]] = {world_hash: [] for world_hash in world_by_hash}
    for run in dataset.runs:
        runs_by_world[run.world_hash].append(run)

    world_recoveries: list[SyntheticWorldRecovery] = []
    reference_by_hash: dict[str, AnalyticalWorldReference] = {}

    for world in worlds:
        reference = analytical_world_reference(world)
        reference_by_hash[world.world_hash] = reference
        runs = runs_by_world[world.world_hash]
        if len(runs) != dataset.replications:
            raise ValueError("each synthetic world must have exactly the declared replications")

        observed_lead = fmean(run.mean_lead_time for run in runs)
        observed_throughput = fmean(run.throughput for run in runs)
        observed_wip = fmean(run.mean_wip for run in runs)

        if reference.expected_mean_lead_time is None:
            relative_error = None
        else:
            relative_error = abs(
                observed_lead - reference.expected_mean_lead_time
            ) / reference.expected_mean_lead_time

        world_recoveries.append(
            SyntheticWorldRecovery(
                world_hash=world.world_hash,
                design_index=world.design_index,
                arm_id=world.arm_id,
                agency_level=world.agency_level.value,
                offered_load=reference.offered_load,
                stable=reference.stable,
                expected_mean_lead_time=reference.expected_mean_lead_time,
                observed_mean_lead_time=observed_lead,
                observed_mean_throughput=observed_throughput,
                observed_mean_wip=observed_wip,
                relative_error_mean_lead_time=relative_error,
            )
        )

    baseline_by_key = {
        (world.design_index, world.agency_level.value): world
        for world in worlds
        if world.arm_id == "baseline"
    }

    effects: list[SyntheticEffectRecovery] = []
    for world in worlds:
        if world.arm_id == "baseline":
            continue
        key = (world.design_index, world.agency_level.value)
        baseline = baseline_by_key.get(key)
        if baseline is None:
            raise ValueError("each intervention world requires a paired baseline world")

        baseline_reference = reference_by_hash[baseline.world_hash]
        intervention_reference = reference_by_hash[world.world_hash]
        baseline_runs = {
            run.replication: run for run in runs_by_world[baseline.world_hash]
        }
        intervention_runs = {
            run.replication: run for run in runs_by_world[world.world_hash]
        }
        if set(baseline_runs) != set(intervention_runs):
            raise ValueError("paired synthetic effects require matching CRN replications")

        simulated_delta = fmean(
            intervention_runs[index].mean_lead_time
            - baseline_runs[index].mean_lead_time
            for index in sorted(baseline_runs)
        )

        if (
            baseline_reference.expected_mean_lead_time is None
            or intervention_reference.expected_mean_lead_time is None
        ):
            expected_delta = None
            null_region = False
            direction_recovered = None
        else:
            expected_delta = (
                intervention_reference.expected_mean_lead_time
                - baseline_reference.expected_mean_lead_time
            )
            null_region = abs(expected_delta) <= equivalence_margin
            direction_recovered = (
                _effect_direction(simulated_delta, equivalence_margin)
                == _effect_direction(expected_delta, equivalence_margin)
            )

        effects.append(
            SyntheticEffectRecovery(
                design_index=world.design_index,
                agency_level=world.agency_level.value,
                arm_id=world.arm_id,
                baseline_world_hash=baseline.world_hash,
                intervention_world_hash=world.world_hash,
                expected_delta_mean_lead_time=expected_delta,
                simulated_delta_mean_lead_time=simulated_delta,
                null_region=null_region,
                direction_recovered=direction_recovered,
            )
        )

    stable_errors = [
        world.relative_error_mean_lead_time
        for world in world_recoveries
        if world.relative_error_mean_lead_time is not None
    ]
    eligible_directions = [
        effect.direction_recovered
        for effect in effects
        if effect.direction_recovered is not None and not effect.null_region
    ]

    return SyntheticRecoveryReport(
        protocol_hash=protocol.protocol_hash,
        dataset_hash=dataset.dataset_hash,
        world_count=len(worlds),
        stable_world_count=sum(world.stable for world in world_recoveries),
        saturated_world_count=sum(not world.stable for world in world_recoveries),
        mean_relative_error_stable=(
            fmean(stable_errors) if stable_errors else 0.0
        ),
        direction_recovery_rate=(
            sum(bool(value) for value in eligible_directions) / len(eligible_directions)
            if eligible_directions
            else 1.0
        ),
        worlds=tuple(world_recoveries),
        effects=tuple(effects),
    )


def _effect_direction(value: float, margin: float) -> int:
    if abs(value) <= margin:
        return 0
    return 1 if value > 0.0 else -1

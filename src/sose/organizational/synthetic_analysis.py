from __future__ import annotations

from hashlib import sha256
import json
from enum import StrEnum
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from .experiment import ExperimentProtocol
from .synthetic_runner import SyntheticExperimentDataset
from .synthetic_study import SyntheticWorldSpec


NonBlankString = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]


class RegimeTransition(StrEnum):
    STABLE_TO_STABLE = "stable_to_stable"
    STABLE_TO_SATURATED = "stable_to_saturated"
    SATURATED_TO_STABLE = "saturated_to_stable"
    SATURATED_TO_SATURATED = "saturated_to_saturated"


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
    regime_transition: RegimeTransition
    expected_delta_mean_lead_time: float | None = Field(default=None, allow_inf_nan=False)
    simulated_delta_mean_lead_time: float | None = Field(default=None, allow_inf_nan=False)
    expected_delta_throughput: float = Field(allow_inf_nan=False)
    simulated_delta_throughput: float = Field(allow_inf_nan=False)
    simulated_delta_operating_cost: float = Field(allow_inf_nan=False)
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

    # Implementation lives in synthetic_recovery_analysis (ADR-0010 split);
    # the public entry point and its import path are unchanged.
    from .synthetic_recovery_analysis import recover_reference_synthetic_experiment

    return recover_reference_synthetic_experiment(
        protocol=protocol, worlds=worlds, dataset=dataset,
    )


def _effect_direction(value: float, margin: float) -> int:
    if abs(value) <= margin:
        return 0
    return 1 if value > 0.0 else -1



def _regime_transition(
    baseline_stable: bool,
    intervention_stable: bool,
) -> RegimeTransition:
    if baseline_stable and intervention_stable:
        return RegimeTransition.STABLE_TO_STABLE
    if baseline_stable and not intervention_stable:
        return RegimeTransition.STABLE_TO_SATURATED
    if not baseline_stable and intervention_stable:
        return RegimeTransition.SATURATED_TO_STABLE
    return RegimeTransition.SATURATED_TO_SATURATED

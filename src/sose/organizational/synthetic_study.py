from __future__ import annotations

from collections.abc import Mapping
from hashlib import sha256
import json
from types import MappingProxyType
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, InstanceOf, StringConstraints, model_validator

from .agency import AgencyLevel, AgencySpec
from .experiment import ExperimentProtocol
from .model_spec import InterventionClass, ModelIntervention, ModelSpec
from .sampling import sample_parameter_space


NonBlankString = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]


class SyntheticWorldSpec(BaseModel):
    """One fully specified synthetic organizational world with known generating inputs."""

    model_config = ConfigDict(extra="forbid", frozen=True, arbitrary_types_allowed=True)

    design_index: int = Field(ge=0)
    arm_id: NonBlankString
    agency_level: AgencyLevel
    crn_group: NonBlankString
    protocol_hash: NonBlankString
    baseline_model_spec_hash: NonBlankString
    exogenous_parameters: dict[str, float]
    intervention_class: InterventionClass | None = None
    intervention_operating_cost: float = Field(default=0.0, ge=0.0, allow_inf_nan=False)
    intervention_transition_cost: float = Field(default=0.0, ge=0.0, allow_inf_nan=False)
    intervention_transition_time: float = Field(default=0.0, ge=0.0, allow_inf_nan=False)
    model_spec: InstanceOf[ModelSpec]
    model_spec_hash: NonBlankString

    @model_validator(mode="after")
    def validate_and_freeze(self) -> "SyntheticWorldSpec":
        if self.model_spec_hash != self.model_spec.model_spec_hash:
            raise ValueError("model_spec_hash must match synthetic world ModelSpec")
        object.__setattr__(
            self,
            "exogenous_parameters",
            MappingProxyType(dict(sorted(self.exogenous_parameters.items()))),
        )
        return self

    def canonical_payload(self) -> dict[str, object]:
        return {
            "design_index": self.design_index,
            "arm_id": self.arm_id,
            "agency_level": self.agency_level.value,
            "crn_group": self.crn_group,
            "protocol_hash": self.protocol_hash,
            "baseline_model_spec_hash": self.baseline_model_spec_hash,
            "exogenous_parameters": dict(self.exogenous_parameters),
            "intervention_class": (
                None if self.intervention_class is None else self.intervention_class.value
            ),
            "intervention_operating_cost": self.intervention_operating_cost,
            "intervention_transition_cost": self.intervention_transition_cost,
            "intervention_transition_time": self.intervention_transition_time,
            "model_spec_hash": self.model_spec_hash,
        }

    @property
    def world_hash(self) -> str:
        payload = json.dumps(
            self.canonical_payload(),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
        return sha256(payload).hexdigest()


def generate_synthetic_worlds(
    *,
    protocol: ExperimentProtocol,
    baseline: ModelSpec,
    interventions: tuple[ModelIntervention, ...],
    agency_specs: Mapping[AgencyLevel, AgencySpec],
    seed: int,
) -> tuple[SyntheticWorldSpec, ...]:
    """Expand a preregistered DOE into synthetic worlds with explicit causal ground truth.

    DOE coordinates are restricted to declared ModelSpec parameters. This keeps
    endogenous execution outputs, such as realized WIP or observed queue fractions,
    out of the regime-vector inputs by construction.
    """

    if baseline.model_spec_hash != protocol.baseline_model_spec_hash:
        raise ValueError("baseline ModelSpec hash does not match experiment protocol")

    declared_parameters = set(baseline.parameters)
    axis_names = set(protocol.parameter_ranges)
    unbound_axes = sorted(axis_names - declared_parameters)
    if unbound_axes:
        raise ValueError(
            "exogenous DOE axes must bind to declared ModelSpec parameters; unbound: "
            + ", ".join(unbound_axes)
        )

    intervention_by_id = {item.intervention_id: item for item in interventions}
    if "baseline" in intervention_by_id:
        raise ValueError("baseline is a reserved synthetic arm identifier")
    if len(intervention_by_id) != len(interventions):
        raise ValueError("duplicate intervention ids are not allowed")
    if set(intervention_by_id) != set(protocol.intervention_ids):
        raise ValueError("intervention ids must exactly match the preregistered protocol")

    missing_agency = [
        level.value for level in protocol.agency_levels if level not in agency_specs
    ]
    if missing_agency:
        raise ValueError(
            "missing agency template for preregistered level(s): "
            + ", ".join(missing_agency)
        )
    for level in protocol.agency_levels:
        if agency_specs[level].level is not level:
            raise ValueError(
                f"agency template for {level.value} declares {agency_specs[level].level.value}"
            )

    points = sample_parameter_space(
        parameter_ranges=protocol.parameter_ranges,
        design=protocol.sampling_design,
        sample_size=protocol.sample_size,
        seed=seed,
    )

    worlds: list[SyntheticWorldSpec] = []
    arms: tuple[tuple[str, ModelIntervention | None], ...] = (
        ("baseline", None),
        *tuple(
            (identifier, intervention_by_id[identifier])
            for identifier in protocol.intervention_ids
        ),
    )

    for design_index, point in enumerate(points):
        point_spec = _with_exogenous_parameters(baseline, point)
        for level in protocol.agency_levels:
            agency_spec = agency_specs[level]
            agency_point_spec = _with_agency(point_spec, agency_spec)
            crn_group = f"design:{design_index}:agency:{level.value}"
            for arm_id, intervention in arms:
                if intervention is None:
                    model_spec = agency_point_spec
                    intervention_class = None
                    operating_cost = 0.0
                    transition_cost = 0.0
                    transition_time = 0.0
                else:
                    model_spec, _ = intervention.apply(agency_point_spec)
                    if model_spec.agency.level is not level:
                        raise ValueError(
                            "intervention cannot rewrite the selected synthetic agency level"
                        )
                    intervention_class = intervention.intervention_class
                    operating_cost = intervention.operating_cost
                    transition_cost = intervention.transition_cost
                    transition_time = intervention.transition_time

                worlds.append(
                    SyntheticWorldSpec(
                        design_index=design_index,
                        arm_id=arm_id,
                        agency_level=level,
                        crn_group=crn_group,
                        protocol_hash=protocol.protocol_hash,
                        baseline_model_spec_hash=baseline.model_spec_hash,
                        exogenous_parameters=point,
                        intervention_class=intervention_class,
                        intervention_operating_cost=operating_cost,
                        intervention_transition_cost=transition_cost,
                        intervention_transition_time=transition_time,
                        model_spec=model_spec,
                        model_spec_hash=model_spec.model_spec_hash,
                    )
                )

    return tuple(worlds)


def _with_exogenous_parameters(
    baseline: ModelSpec,
    values: Mapping[str, float],
) -> ModelSpec:
    payload = baseline.canonical_payload()
    parameters = dict(payload["parameters"])
    parameters.update(values)
    payload["parameters"] = parameters
    return ModelSpec.model_validate(payload)


def _with_agency(spec: ModelSpec, agency: AgencySpec) -> ModelSpec:
    payload = spec.canonical_payload()
    payload["agency"] = agency.canonical_payload()
    return ModelSpec.model_validate(payload)

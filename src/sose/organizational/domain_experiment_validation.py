"""World-construction input validation (split from domain_experiment)."""
from __future__ import annotations

from collections.abc import Mapping
from math import isfinite
from typing import TYPE_CHECKING

from .agency import AgencyLevel
from .domain_reference import AgencyCapabilitySpec, DomainReferenceDescriptor
from .experiment import ExperimentProtocol
from .model_spec import ModelIntervention, ModelSpec

if TYPE_CHECKING:
    from .domain_experiment import AgencyConfiguration


def _validate_protocol_parameter_space(
    *,
    protocol: ExperimentProtocol,
    parameter_by_name: Mapping[str, object],
) -> None:
    undeclared = sorted(set(protocol.parameter_ranges) - set(parameter_by_name))
    if undeclared:
        raise ValueError(
            "DOE axes must be declared domain parameters; undeclared: "
            + ", ".join(undeclared)
        )

    for name, requested in protocol.parameter_ranges.items():
        definition = parameter_by_name[name]
        allowed = definition.range
        if requested.low < allowed.low or requested.high > allowed.high:
            raise ValueError(
                f"DOE range for {name} is outside the domain-declared range"
            )


def _interventions_by_id(
    interventions: tuple[ModelIntervention, ...], protocol: ExperimentProtocol,
) -> dict[str, ModelIntervention]:
    intervention_by_id = {item.intervention_id: item for item in interventions}
    if "baseline" in intervention_by_id:
        raise ValueError("baseline is a reserved experiment arm identifier")
    if len(intervention_by_id) != len(interventions):
        raise ValueError("duplicate intervention ids are not allowed")
    unknown_interventions = sorted(set(protocol.intervention_ids) - set(intervention_by_id))
    if unknown_interventions:
        raise ValueError(
            "experiment protocol requests unknown intervention id(s): "
            + ", ".join(unknown_interventions)
        )
    return intervention_by_id


def _validate_agency_configurations(
    *,
    protocol: ExperimentProtocol,
    descriptor: DomainReferenceDescriptor,
    configurations: tuple[AgencyConfiguration, ...],
) -> dict[AgencyLevel, AgencyConfiguration]:
    by_level: dict[AgencyLevel, AgencyConfiguration] = {}
    for configuration in configurations:
        if configuration.level in by_level:
            raise ValueError("only one agency configuration per level is allowed")
        by_level[configuration.level] = configuration

    if set(by_level) != set(protocol.agency_levels):
        raise ValueError(
            "agency configurations must exactly match protocol agency levels"
        )

    capability_by_key: dict[tuple[AgencyLevel, str], AgencyCapabilitySpec] = {
        (capability.level, capability.capability_id): capability
        for capability in descriptor.agency_capabilities
    }
    for configuration in configurations:
        key = (configuration.level, configuration.capability_id)
        _require_declared_capability(configuration, capability_by_key.get(key))
    return by_level


def _require_declared_capability(
    configuration: AgencyConfiguration, capability: AgencyCapabilitySpec | None,
) -> None:
    if capability is None:
        raise ValueError("selected agency capability is not declared by the domain reference")
    if set(configuration.parameters) != set(capability.parameter_ranges):
        raise ValueError(
            "agency configuration parameters must exactly match the capability schema"
        )
    for name, value in configuration.parameters.items():
        bounds = capability.parameter_ranges[name]
        if value < bounds.low or value > bounds.high:
            raise ValueError(f"agency parameter {name} is outside its declared range")


def _validate_built_model(
    *,
    model: ModelSpec,
    configured_point: Mapping[str, float],
) -> None:
    for name, expected in configured_point.items():
        actual = model.parameters.get(name)
        if (
            isinstance(actual, bool)
            or not isinstance(actual, (int, float))
            or not isfinite(float(actual))
            or float(actual) != expected
        ):
            raise ValueError(
                "domain build_model must preserve the configured exogenous point "
                f"for parameter {name}"
            )

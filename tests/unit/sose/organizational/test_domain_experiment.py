from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType

import pytest
from pydantic import ValidationError

from sose.organizational.agency import AgencyLevel
from sose.organizational.domain_experiment import (
    AgencyConfiguration,
    ExperimentWorld,
    build_experiment_worlds,
)
from sose.organizational.domain_reference import (
    AgencyCapabilitySpec,
    DomainReferenceDescriptor,
    DomainReferenceIdentity,
    GroundTruthKind,
    ParameterDefinition,
)
from sose.organizational.experiment import (
    ExperimentProtocol,
    FalsificationRule,
    MetricDirection,
    MultipleComparisonMethod,
    OutcomeMetric,
    ParameterRange,
    ReplicationPlan,
    SamplingDesign,
    StatisticalPlan,
)
from sose.organizational.model_spec import (
    EvidenceClass,
    InterventionClass,
    ModelIntervention,
    ModelSpec,
)


@dataclass(frozen=True)
class FakeReference:
    descriptor: DomainReferenceDescriptor
    _interventions: tuple[ModelIntervention, ...]

    def build_model(self, point: dict[str, float]) -> ModelSpec:
        return ModelSpec(
            demand={"kind": "synthetic_open_system"},
            costs={"operating_cost": 1.0},
            parameters=dict(point),
            parameter_evidence={
                name: EvidenceClass.ASSUMED
                for name in point
            },
        )

    def interventions(self) -> tuple[ModelIntervention, ...]:
        return self._interventions


@dataclass(frozen=True)
class BadReference(FakeReference):
    def build_model(self, point: dict[str, float]) -> ModelSpec:
        return ModelSpec(
            demand={"kind": "synthetic_open_system"},
            costs={"operating_cost": 1.0},
            parameters={
                "arrival_rate": 0.8,
                "service_capacity": 1.0,
                "rework_probability": 0.15,
            },
            parameter_evidence={
                "arrival_rate": EvidenceClass.ASSUMED,
                "service_capacity": EvidenceClass.ASSUMED,
                "rework_probability": EvidenceClass.ASSUMED,
            },
        )


def _descriptor() -> DomainReferenceDescriptor:
    return DomainReferenceDescriptor(
        identity=DomainReferenceIdentity(
            domain="manufacturing",
            reference_id="manufacturing-reference",
            reference_version="1",
            process_manifest_domain="manufacturing",
            specification_path="docs/examples/manufacturing/specification.md",
        ),
        parameters=(
            ParameterDefinition(
                name="arrival_rate",
                description="External arrival intensity.",
                units="items/hour",
                default=0.8,
                range=ParameterRange(low=0.2, high=1.4),
                evidence_class=EvidenceClass.ASSUMED,
            ),
            ParameterDefinition(
                name="service_capacity",
                description="Configured nominal service capacity.",
                units="items/hour",
                default=1.0,
                range=ParameterRange(low=0.5, high=2.0),
                evidence_class=EvidenceClass.ASSUMED,
            ),
            ParameterDefinition(
                name="rework_probability",
                description="Configured rework probability.",
                default=0.15,
                range=ParameterRange(low=0.05, high=0.4),
                evidence_class=EvidenceClass.ASSUMED,
            ),
        ),
        agency_capabilities=(
            AgencyCapabilitySpec(
                level=AgencyLevel.A0,
                capability_id="fixed-policy",
            ),
            AgencyCapabilitySpec(
                level=AgencyLevel.A1,
                capability_id="adaptive-batching",
                parameter_ranges={
                    "backlog_trigger": ParameterRange(low=0.0, high=10.0),
                    "service_multiplier": ParameterRange(low=0.5, high=1.0),
                },
                defaults={
                    "backlog_trigger": 2.0,
                    "service_multiplier": 0.7,
                },
            ),
        ),
        ground_truth_kinds=(
            GroundTruthKind.ANALYTICAL,
            GroundTruthKind.MECHANISTIC,
        ),
    )


def _interventions() -> tuple[ModelIntervention, ...]:
    return (
        ModelIntervention(
            intervention_id="capacity-up",
            intervention_class=InterventionClass.CAPACITY,
            set_values={"/parameters/service_capacity": 1.6},
            operating_cost=0.35,
            transition_cost=2.0,
        ),
        ModelIntervention(
            intervention_id="automation-rework",
            intervention_class=InterventionClass.AUTOMATION,
            set_values={"/parameters/rework_probability": 0.02},
            operating_cost=0.2,
            transition_cost=1.0,
        ),
    )


def _reference() -> FakeReference:
    return FakeReference(
        descriptor=_descriptor(),
        _interventions=_interventions(),
    )


def _agency() -> tuple[AgencyConfiguration, ...]:
    return (
        AgencyConfiguration(
            level=AgencyLevel.A0,
            capability_id="fixed-policy",
        ),
        AgencyConfiguration(
            level=AgencyLevel.A1,
            capability_id="adaptive-batching",
            parameters={
                "backlog_trigger": 2.0,
                "service_multiplier": 0.7,
            },
        ),
    )


def _protocol(reference: FakeReference) -> ExperimentProtocol:
    defaults = {
        parameter.name: parameter.default
        for parameter in reference.descriptor.parameters
    }
    baseline = reference.build_model(defaults)
    return ExperimentProtocol(
        research_question="Can the generic framework construct comparable worlds?",
        baseline_model_spec_hash=baseline.model_spec_hash,
        intervention_ids=("capacity-up", "automation-rework"),
        agency_levels=(AgencyLevel.A0, AgencyLevel.A1),
        parameter_ranges={
            "arrival_rate": ParameterRange(low=0.4, high=1.2),
            "service_capacity": ParameterRange(low=0.8, high=1.2),
        },
        sampling_design=SamplingDesign.LATIN_HYPERCUBE,
        sample_size=2,
        outcomes=(
            OutcomeMetric(
                name="lead_time",
                direction=MetricDirection.MINIMIZE,
                equivalence_margin=0.05,
            ),
        ),
        statistical_plan=StatisticalPlan(
            confidence_level=0.95,
            multiple_comparison=MultipleComparisonMethod.HOLM,
        ),
        replication_plan=ReplicationPlan(
            min_replications=2,
            max_replications=2,
            target_ci_half_width=0.1,
        ),
        warmup=10.0,
        horizon=100.0,
        falsification=FalsificationRule(
            primary_metric="lead_time",
            theta_fraction=0.5,
        ),
    )


def test_framework_owns_world_expansion_and_crn_grouping() -> None:
    reference = _reference()
    protocol = _protocol(reference)

    worlds = build_experiment_worlds(
        reference=reference,
        protocol=protocol,
        agency_configurations=_agency(),
        design_seed=20261008,
    )

    assert len(worlds) == 2 * 3 * 2
    assert all(isinstance(world, ExperimentWorld) for world in worlds)

    by_design: dict[int, list[ExperimentWorld]] = {}
    for world in worlds:
        by_design.setdefault(world.design_index, []).append(world)

    assert set(by_design) == {0, 1}
    assert all(len(items) == 6 for items in by_design.values())
    assert all(
        len({world.crn_group for world in items}) == 1
        for items in by_design.values()
    )

    identities = {
        (world.design_index, world.arm_id, world.agency_configuration.level)
        for world in worlds
    }
    assert len(identities) == len(worlds)


def test_agency_changes_world_identity_without_rewriting_structural_model() -> None:
    reference = _reference()
    worlds = build_experiment_worlds(
        reference=reference,
        protocol=_protocol(reference),
        agency_configurations=_agency(),
        design_seed=11,
    )

    a0 = next(
        world
        for world in worlds
        if world.design_index == 0
        and world.arm_id == "baseline"
        and world.agency_configuration.level is AgencyLevel.A0
    )
    a1 = next(
        world
        for world in worlds
        if world.design_index == 0
        and world.arm_id == "baseline"
        and world.agency_configuration.level is AgencyLevel.A1
    )

    assert a0.model_spec_hash == a1.model_spec_hash
    assert a0.structural_configuration_hash == a1.structural_configuration_hash
    assert a0.agency_configuration_hash != a1.agency_configuration_hash
    assert a0.world_hash != a1.world_hash


def test_world_construction_is_deterministic_hash_addressed_and_json_serializable() -> None:
    reference = _reference()
    protocol = _protocol(reference)

    left = build_experiment_worlds(
        reference=reference,
        protocol=protocol,
        agency_configurations=_agency(),
        design_seed=77,
    )
    right = build_experiment_worlds(
        reference=reference,
        protocol=protocol,
        agency_configurations=tuple(reversed(_agency())),
        design_seed=77,
    )

    assert [world.world_hash for world in left] == [world.world_hash for world in right]
    assert left[0].model_dump(mode="json")["exogenous_parameters"]
    assert left[0].model_dump_json()


def test_framework_rejects_undeclared_or_out_of_bounds_doe_axes() -> None:
    reference = _reference()
    protocol = _protocol(reference)

    undeclared = protocol.model_copy(
        update={
            "parameter_ranges": MappingProxyType(
                {
                    **dict(protocol.parameter_ranges),
                    "realized_wip": ParameterRange(low=0.0, high=10.0),
                }
            )
        }
    )
    with pytest.raises(ValueError, match="declared domain parameters"):
        build_experiment_worlds(
            reference=reference,
            protocol=undeclared,
            agency_configurations=_agency(),
            design_seed=1,
        )

    out_of_bounds = protocol.model_copy(
        update={
            "parameter_ranges": MappingProxyType(
                {
                    **dict(protocol.parameter_ranges),
                    "arrival_rate": ParameterRange(low=0.1, high=1.3),
                }
            )
        }
    )
    with pytest.raises(ValueError, match="outside the domain-declared range"):
        build_experiment_worlds(
            reference=reference,
            protocol=out_of_bounds,
            agency_configurations=_agency(),
            design_seed=1,
        )


def test_framework_rejects_reference_that_ignores_sampled_exogenous_point() -> None:
    good = _reference()
    bad = BadReference(
        descriptor=good.descriptor,
        _interventions=good._interventions,
    )

    with pytest.raises(ValueError, match="must preserve the configured exogenous point"):
        build_experiment_worlds(
            reference=bad,
            protocol=_protocol(good),
            agency_configurations=_agency(),
            design_seed=3,
        )


def test_framework_validates_selected_agency_capability_before_world_construction() -> None:
    reference = _reference()
    protocol = _protocol(reference)

    with pytest.raises(ValueError, match="agency configurations must exactly match"):
        build_experiment_worlds(
            reference=reference,
            protocol=protocol,
            agency_configurations=(_agency()[0],),
            design_seed=4,
        )

    invalid = AgencyConfiguration(
        level=AgencyLevel.A1,
        capability_id="adaptive-batching",
        parameters={
            "backlog_trigger": 2.0,
            "service_multiplier": 1.2,
        },
    )
    with pytest.raises(ValueError, match="outside its declared range"):
        build_experiment_worlds(
            reference=reference,
            protocol=protocol,
            agency_configurations=(_agency()[0], invalid),
            design_seed=4,
        )


def test_intervention_hash_is_canonical_and_worlds_bind_it() -> None:
    left = ModelIntervention(
        intervention_id="capacity-up",
        intervention_class=InterventionClass.CAPACITY,
        set_values={
            "/parameters/service_capacity": 1.6,
            "/costs/capacity_delta": 0.35,
        },
    )
    right = ModelIntervention(
        intervention_id="capacity-up",
        intervention_class=InterventionClass.CAPACITY,
        set_values={
            "/costs/capacity_delta": 0.35,
            "/parameters/service_capacity": 1.6,
        },
    )
    assert left.intervention_hash == right.intervention_hash

    reference = _reference()
    worlds = build_experiment_worlds(
        reference=reference,
        protocol=_protocol(reference),
        agency_configurations=_agency(),
        design_seed=5,
    )
    capacity = next(world for world in worlds if world.arm_id == "capacity-up")
    baseline = next(world for world in worlds if world.arm_id == "baseline")

    assert capacity.intervention_hash == reference._interventions[0].intervention_hash
    assert baseline.intervention_hash is None



def test_protocol_may_select_a_supported_intervention_subset() -> None:
    reference = _reference()
    protocol = _protocol(reference).model_copy(
        update={"intervention_ids": ("capacity-up",)}
    )

    worlds = build_experiment_worlds(
        reference=reference,
        protocol=protocol,
        agency_configurations=_agency(),
        design_seed=6,
    )

    assert {world.arm_id for world in worlds} == {"baseline", "capacity-up"}


def test_protocol_rejects_unknown_intervention_id() -> None:
    reference = _reference()
    protocol = _protocol(reference).model_copy(
        update={"intervention_ids": ("capacity-up", "unknown-arm")}
    )

    with pytest.raises(ValueError, match="unknown intervention"):
        build_experiment_worlds(
            reference=reference,
            protocol=protocol,
            agency_configurations=_agency(),
            design_seed=6,
        )

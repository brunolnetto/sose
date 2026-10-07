from __future__ import annotations

from sose.organizational.agency import (
    A1Policy,
    A1PolicyName,
    A2ObservationModel,
    AgencyLevel,
    AgencySpec,
)
from sose.organizational.experiment import (
    CostAnalysisPlan,
    ExperimentProtocol,
    FalsificationRule,
    MetricDirection,
    MultipleComparisonMethod,
    OutcomeMetric,
    ParameterRange,
    ReplicationPlan,
    SamplingDesign,
    StatisticalPlan,
    SurrogateAnalysisPlan,
    SurrogateModel,
)
from sose.organizational.model_spec import (
    EvidenceClass,
    InterventionClass,
    ModelIntervention,
    ModelSpec,
)
from sose.organizational.synthetic_study import generate_synthetic_worlds


def _baseline() -> ModelSpec:
    return ModelSpec(
        parameters={
            "arrival_rate": 0.8,
            "service_capacity": 1.0,
            "service_cv": 1.0,
            "rework_probability": 0.1,
            "transition_cost": 0.0,
        },
        parameter_evidence={
            "arrival_rate": EvidenceClass.ASSUMED,
            "service_capacity": EvidenceClass.ASSUMED,
            "service_cv": EvidenceClass.ASSUMED,
            "rework_probability": EvidenceClass.ASSUMED,
            "transition_cost": EvidenceClass.ASSUMED,
        },
        demand={"kind": "synthetic_open_system"},
        costs={"operating_cost": 1.0},
    )


def _protocol(baseline: ModelSpec) -> ExperimentProtocol:
    return ExperimentProtocol(
        protocol_version="2",
        research_question="Recover intervention effects from synthetic organizational worlds.",
        baseline_model_spec_hash=baseline.model_spec_hash,
        intervention_ids=("capacity-up", "automation"),
        agency_levels=(AgencyLevel.A0, AgencyLevel.A1, AgencyLevel.A2),
        parameter_ranges={
            "arrival_rate": ParameterRange(low=0.4, high=1.4),
            "service_capacity": ParameterRange(low=0.7, high=1.5),
            "service_cv": ParameterRange(low=0.4, high=2.0),
            "rework_probability": ParameterRange(low=0.0, high=0.35),
            "transition_cost": ParameterRange(low=0.0, high=20.0),
        },
        sampling_design=SamplingDesign.LATIN_HYPERCUBE,
        sample_size=6,
        outcomes=(
            OutcomeMetric(
                name="lead_time",
                direction=MetricDirection.MINIMIZE,
                equivalence_margin=0.05,
            ),
            OutcomeMetric(
                name="operating_cost",
                direction=MetricDirection.MINIMIZE,
                equivalence_margin=0.05,
            ),
        ),
        statistical_plan=StatisticalPlan(
            confidence_level=0.95,
            multiple_comparison=MultipleComparisonMethod.HOLM,
        ),
        replication_plan=ReplicationPlan(
            min_replications=3,
            max_replications=8,
            target_ci_half_width=0.1,
        ),
        cost_analysis=CostAnalysisPlan(
            operating_cost_metric="operating_cost",
            transition_cost_parameter="transition_cost",
        ),
        surrogate_analysis=SurrogateAnalysisPlan(
            model=SurrogateModel.GENERALIZED_ADDITIVE,
        ),
        warmup=100.0,
        horizon=1000.0,
        falsification=FalsificationRule(
            primary_metric="lead_time",
            theta_fraction=0.5,
        ),
    )


def _interventions() -> tuple[ModelIntervention, ...]:
    return (
        ModelIntervention(
            intervention_id="capacity-up",
            intervention_class=InterventionClass.CAPACITY,
            set_values={"/parameters/service_capacity": 1.8},
            operating_cost=4.0,
            transition_cost=8.0,
        ),
        ModelIntervention(
            intervention_id="automation",
            intervention_class=InterventionClass.AUTOMATION,
            set_values={"/parameters/rework_probability": 0.02},
            operating_cost=2.0,
            transition_cost=12.0,
        ),
    )


def _agency_specs() -> dict[AgencyLevel, AgencySpec]:
    return {
        AgencyLevel.A0: AgencySpec(level=AgencyLevel.A0),
        AgencyLevel.A1: AgencySpec(
            level=AgencyLevel.A1,
            a1_policy=A1Policy(
                name=A1PolicyName.QUEUE_BYPASS,
                parameters={"threshold": 5},
            ),
        ),
        AgencyLevel.A2: AgencySpec(
            level=AgencyLevel.A2,
            a2_observation=A2ObservationModel(
                metrics=("lead_time", "wip"),
                aggregation_window=20.0,
                observation_delay=5.0,
                measurement_noise_std=0.1,
                trigger_rule="lead_time > target",
                cooldown=10.0,
                permitted_actions=("capacity-up",),
            ),
        ),
    }


def test_synthetic_world_design_is_deterministic_and_hash_addressed() -> None:
    baseline = _baseline()
    protocol = _protocol(baseline)

    left = generate_synthetic_worlds(
        protocol=protocol,
        baseline=baseline,
        interventions=_interventions(),
        agency_specs=_agency_specs(),
        seed=20261007,
    )
    right = generate_synthetic_worlds(
        protocol=protocol,
        baseline=baseline,
        interventions=_interventions(),
        agency_specs=_agency_specs(),
        seed=20261007,
    )

    assert left == right
    assert tuple(world.world_hash for world in left) == tuple(world.world_hash for world in right)
    assert len(set(world.world_hash for world in left)) == len(left)


def test_design_is_cartesian_over_points_arms_and_agency_with_shared_crn_groups() -> None:
    baseline = _baseline()
    protocol = _protocol(baseline)
    worlds = generate_synthetic_worlds(
        protocol=protocol,
        baseline=baseline,
        interventions=_interventions(),
        agency_specs=_agency_specs(),
        seed=9,
    )

    assert len(worlds) == 6 * 3 * 3
    first_group = [
        world
        for world in worlds
        if world.design_index == 0 and world.agency_level is AgencyLevel.A0
    ]
    assert {world.arm_id for world in first_group} == {"baseline", "capacity-up", "automation"}
    assert len({world.crn_group for world in first_group}) == 1


def test_protocol_axes_bind_only_to_declared_model_parameters() -> None:
    baseline = _baseline()
    protocol = _protocol(baseline).model_copy(
        update={
            "parameter_ranges": {
                **dict(_protocol(baseline).parameter_ranges),
                "endogenous_queue_fraction": ParameterRange(low=0.0, high=1.0),
            }
        }
    )

    try:
        generate_synthetic_worlds(
            protocol=protocol,
            baseline=baseline,
            interventions=_interventions(),
            agency_specs=_agency_specs(),
            seed=1,
        )
    except ValueError as exc:
        assert "exogenous DOE axes" in str(exc)
        assert "endogenous_queue_fraction" in str(exc)
    else:
        raise AssertionError("expected undeclared/endogenous axis rejection")


def test_generated_worlds_preserve_ground_truth_inputs_and_apply_intervention_and_agency() -> None:
    baseline = _baseline()
    protocol = _protocol(baseline)
    worlds = generate_synthetic_worlds(
        protocol=protocol,
        baseline=baseline,
        interventions=_interventions(),
        agency_specs=_agency_specs(),
        seed=12,
    )

    world = next(
        candidate
        for candidate in worlds
        if candidate.design_index == 0
        and candidate.arm_id == "capacity-up"
        and candidate.agency_level is AgencyLevel.A1
    )

    assert set(world.exogenous_parameters) == set(protocol.parameter_ranges)
    assert world.model_spec.parameters["service_capacity"] == 1.8
    assert world.model_spec.agency.level is AgencyLevel.A1
    assert world.intervention_class is InterventionClass.CAPACITY
    assert world.baseline_model_spec_hash == baseline.model_spec_hash
    assert world.protocol_hash == protocol.protocol_hash
    assert world.model_spec_hash == world.model_spec.model_spec_hash


def test_design_rejects_wrong_baseline_intervention_set_and_missing_agency_template() -> None:
    baseline = _baseline()
    protocol = _protocol(baseline)

    wrong_baseline = baseline.model_copy(
        update={"demand": {"kind": "different"}}
    )
    for kwargs, expected in (
        ({"baseline": wrong_baseline}, "baseline ModelSpec hash"),
        ({"interventions": _interventions()[:1]}, "intervention ids"),
        (
            {
                "agency_specs": {
                    AgencyLevel.A0: _agency_specs()[AgencyLevel.A0],
                    AgencyLevel.A1: _agency_specs()[AgencyLevel.A1],
                }
            },
            "agency template",
        ),
    ):
        args = {
            "protocol": protocol,
            "baseline": baseline,
            "interventions": _interventions(),
            "agency_specs": _agency_specs(),
            "seed": 1,
        }
        args.update(kwargs)
        try:
            generate_synthetic_worlds(**args)
        except ValueError as exc:
            assert expected in str(exc)
        else:
            raise AssertionError("expected synthetic design validation error")


def test_design_reserves_baseline_arm_identifier() -> None:
    baseline = _baseline()
    protocol = _protocol(baseline).model_copy(
        update={"intervention_ids": ("baseline", "automation")}
    )
    interventions = (
        ModelIntervention(
            intervention_id="baseline",
            intervention_class=InterventionClass.CAPACITY,
            set_values={"/parameters/service_capacity": 2.0},
        ),
        _interventions()[1],
    )

    try:
        generate_synthetic_worlds(
            protocol=protocol,
            baseline=baseline,
            interventions=interventions,
            agency_specs=_agency_specs(),
            seed=1,
        )
    except ValueError as exc:
        assert "baseline" in str(exc)
        assert "reserved" in str(exc)
    else:
        raise AssertionError("expected reserved baseline arm rejection")


def test_interventions_cannot_rewrite_selected_agency_axis() -> None:
    baseline = _baseline()
    protocol = _protocol(baseline)
    interventions = (
        ModelIntervention(
            intervention_id="capacity-up",
            intervention_class=InterventionClass.CAPACITY,
            set_values={
                "/parameters/service_capacity": 1.8,
                "/agency": AgencySpec(level=AgencyLevel.A0).canonical_payload(),
            },
        ),
        _interventions()[1],
    )

    try:
        generate_synthetic_worlds(
            protocol=protocol,
            baseline=baseline,
            interventions=interventions,
            agency_specs=_agency_specs(),
            seed=1,
        )
    except ValueError as exc:
        assert "agency level" in str(exc)
    else:
        raise AssertionError("expected intervention agency-axis rewrite rejection")

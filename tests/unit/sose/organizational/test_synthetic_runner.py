from __future__ import annotations

from sose.organizational.agency import AgencyLevel, AgencySpec
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
from sose.organizational.synthetic_runner import run_reference_synthetic_experiment
from sose.organizational.synthetic_study import generate_synthetic_worlds


def _baseline() -> ModelSpec:
    return ModelSpec(
        parameters={
            "arrival_rate": 0.8,
            "service_capacity": 1.0,
            "service_cv": 0.7,
            "rework_probability": 0.15,
            "transition_cost": 0.0,
        },
        parameter_evidence={
            name: EvidenceClass.ASSUMED
            for name in (
                "arrival_rate",
                "service_capacity",
                "service_cv",
                "rework_probability",
                "transition_cost",
            )
        },
        demand={"kind": "synthetic_open_system"},
        costs={"operating_cost": 1.5},
    )


def _protocol(baseline: ModelSpec) -> ExperimentProtocol:
    return ExperimentProtocol(
        protocol_version="2",
        research_question="Synthetic effect recovery.",
        baseline_model_spec_hash=baseline.model_spec_hash,
        intervention_ids=("capacity-up", "automation"),
        agency_levels=(AgencyLevel.A0,),
        parameter_ranges={
            "arrival_rate": ParameterRange(low=0.6, high=1.0),
            "service_capacity": ParameterRange(low=0.9, high=1.2),
            "service_cv": ParameterRange(low=0.4, high=1.0),
            "rework_probability": ParameterRange(low=0.08, high=0.25),
            "transition_cost": ParameterRange(low=0.0, high=5.0),
        },
        sampling_design=SamplingDesign.LATIN_HYPERCUBE,
        sample_size=2,
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
            min_replications=2,
            max_replications=4,
            target_ci_half_width=0.1,
        ),
        cost_analysis=CostAnalysisPlan(
            operating_cost_metric="operating_cost",
            transition_cost_parameter="transition_cost",
        ),
        surrogate_analysis=SurrogateAnalysisPlan(model=SurrogateModel.LINEAR),
        warmup=5.0,
        horizon=60.0,
        falsification=FalsificationRule(
            primary_metric="lead_time",
            theta_fraction=0.5,
        ),
    )


def _worlds():
    baseline = _baseline()
    protocol = _protocol(baseline)
    interventions = (
        ModelIntervention(
            intervention_id="capacity-up",
            intervention_class=InterventionClass.CAPACITY,
            set_values={"/parameters/service_capacity": 1.8},
            operating_cost=0.4,
            transition_cost=2.0,
        ),
        ModelIntervention(
            intervention_id="automation",
            intervention_class=InterventionClass.AUTOMATION,
            set_values={"/parameters/rework_probability": 0.02},
            operating_cost=0.2,
            transition_cost=3.0,
        ),
    )
    worlds = generate_synthetic_worlds(
        protocol=protocol,
        baseline=baseline,
        interventions=interventions,
        agency_specs={AgencyLevel.A0: AgencySpec(level=AgencyLevel.A0)},
        seed=123,
    )
    return protocol, worlds


def test_reference_experiment_is_deterministic_and_hash_addressed() -> None:
    protocol, worlds = _worlds()

    left = run_reference_synthetic_experiment(
        protocol=protocol,
        worlds=worlds,
        root_seed=20261007,
    )
    right = run_reference_synthetic_experiment(
        protocol=protocol,
        worlds=worlds,
        root_seed=20261007,
    )

    assert left == right
    assert left.dataset_hash == right.dataset_hash
    assert len(left.runs) == len(worlds) * protocol.replication_plan.min_replications


def test_crn_preserves_arrival_and_latent_service_streams_across_arms() -> None:
    protocol, worlds = _worlds()
    dataset = run_reference_synthetic_experiment(
        protocol=protocol,
        worlds=worlds,
        root_seed=77,
        replications=2,
    )

    baseline = next(
        run
        for run in dataset.runs
        if run.design_index == 0 and run.arm_id == "baseline" and run.replication == 0
    )
    capacity = next(
        run
        for run in dataset.runs
        if run.design_index == 0 and run.arm_id == "capacity-up" and run.replication == 0
    )

    count = min(len(baseline.items), len(capacity.items))
    assert count > 5
    assert [item.arrival_at for item in baseline.items[:count]] == [
        item.arrival_at for item in capacity.items[:count]
    ]
    assert [item.service_latent_normal for item in baseline.items[:count]] == [
        item.service_latent_normal for item in capacity.items[:count]
    ]
    assert [item.rework_latent_uniform for item in baseline.items[:count]] == [
        item.rework_latent_uniform for item in capacity.items[:count]
    ]


def test_reference_run_emits_item_level_synthetic_data_and_derived_outcomes() -> None:
    protocol, worlds = _worlds()
    dataset = run_reference_synthetic_experiment(
        protocol=protocol,
        worlds=worlds,
        root_seed=99,
        replications=2,
    )
    run = dataset.runs[0]

    assert run.items
    assert run.completed_items <= run.arrived_items
    assert run.throughput >= 0.0
    assert run.mean_wip >= 0.0
    assert run.mean_lead_time >= 0.0
    assert run.p90_lead_time >= run.median_lead_time
    assert 0.0 <= run.rework_fraction <= 1.0
    assert run.total_operating_cost >= 0.0
    assert all(
        item.lead_time == item.queue_time + item.processing_time + item.rework_time
        for item in run.items
    )


def test_runner_rejects_protocol_world_mismatch_and_replication_bounds() -> None:
    protocol, worlds = _worlds()

    for kwargs, expected in (
        ({"replications": 1}, "replications"),
        ({"replications": 5}, "replications"),
        ({"worlds": worlds[:-1]}, "complete synthetic world design"),
    ):
        args = {
            "protocol": protocol,
            "worlds": worlds,
            "root_seed": 1,
            "replications": 2,
        }
        args.update(kwargs)
        try:
            run_reference_synthetic_experiment(**args)
        except ValueError as exc:
            assert expected in str(exc)
        else:
            raise AssertionError("expected synthetic runner validation error")


def test_reference_runner_refuses_unimplemented_agency_mechanics() -> None:
    protocol, worlds = _worlds()
    forged = worlds[0].model_copy(update={"agency_level": AgencyLevel.A1})

    try:
        run_reference_synthetic_experiment(
            protocol=protocol,
            worlds=(forged, *worlds[1:]),
            root_seed=1,
            replications=2,
        )
    except ValueError as exc:
        assert "A0" in str(exc)
        assert "reference" in str(exc)
    else:
        raise AssertionError("expected unsupported agency-level rejection")


def test_throughput_counts_measurement_window_departures_from_pre_warmup_arrivals() -> None:
    protocol, worlds = _worlds()
    dataset = run_reference_synthetic_experiment(
        protocol=protocol,
        worlds=worlds,
        root_seed=1234,
        replications=2,
    )
    run = dataset.runs[0]

    window_departures = sum(
        protocol.warmup < item.completed_at <= protocol.horizon
        for item in run.items
    )
    assert run.throughput == window_departures / (protocol.horizon - protocol.warmup)


def test_cost_uses_preregistered_transition_cost_parameter_name() -> None:
    baseline = _baseline()
    protocol = _protocol(baseline).model_copy(
        update={
            "parameter_ranges": {
                **dict(_protocol(baseline).parameter_ranges),
                "migration_cost": ParameterRange(low=2.0, high=4.0),
            },
            "cost_analysis": CostAnalysisPlan(
                operating_cost_metric="operating_cost",
                transition_cost_parameter="migration_cost",
            ),
        }
    )
    updated_baseline = ModelSpec(
        parameters={
            **dict(baseline.parameters),
            "migration_cost": 3.0,
        },
        parameter_evidence={
            **dict(baseline.parameter_evidence),
            "migration_cost": EvidenceClass.ASSUMED,
        },
        demand=dict(baseline.demand),
        costs=dict(baseline.costs),
    )
    protocol = protocol.model_copy(
        update={"baseline_model_spec_hash": updated_baseline.model_spec_hash}
    )
    worlds = generate_synthetic_worlds(
        protocol=protocol,
        baseline=updated_baseline,
        interventions=(
            ModelIntervention(
                intervention_id="capacity-up",
                intervention_class=InterventionClass.CAPACITY,
                set_values={"/parameters/service_capacity": 1.8},
                transition_cost=2.0,
            ),
            ModelIntervention(
                intervention_id="automation",
                intervention_class=InterventionClass.AUTOMATION,
                set_values={"/parameters/rework_probability": 0.02},
                transition_cost=3.0,
            ),
        ),
        agency_specs={AgencyLevel.A0: AgencySpec(level=AgencyLevel.A0)},
        seed=8,
    )
    dataset = run_reference_synthetic_experiment(
        protocol=protocol,
        worlds=worlds,
        root_seed=7,
        replications=2,
    )

    baseline_run = next(run for run in dataset.runs if run.arm_id == "baseline")
    capacity_run = next(run for run in dataset.runs if run.arm_id == "capacity-up")

    uncertain = next(
        world.exogenous_parameters["migration_cost"]
        for world in worlds
        if world.world_hash == capacity_run.world_hash
    )
    measurement = protocol.horizon - protocol.warmup
    assert baseline_run.total_operating_cost == baseline_run.total_operating_cost
    assert capacity_run.total_operating_cost >= uncertain + 2.0 + measurement * 1.5


def test_runner_requires_exact_cartesian_world_identities() -> None:
    protocol, worlds = _worlds()
    forged = worlds[-1].model_copy(update={"design_index": 999})
    malformed = (*worlds[:-1], forged)

    try:
        run_reference_synthetic_experiment(
            protocol=protocol,
            worlds=malformed,
            root_seed=1,
            replications=2,
        )
    except ValueError as exc:
        assert "complete synthetic world design" in str(exc)
    else:
        raise AssertionError("expected exact Cartesian design rejection")

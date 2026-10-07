from __future__ import annotations

from math import isclose

from sose.organizational.agency import A1Policy, A1PolicyName, AgencyLevel, AgencySpec
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
from sose.organizational.ledger import ActorCategory
from sose.organizational.model_spec import EvidenceClass, ModelSpec
from sose.organizational.synthetic_a1 import (
    A1AdaptivePolicyParameters,
    run_a1_reference_world,
)
from sose.organizational.synthetic_study import SyntheticWorldSpec


def _protocol(spec: ModelSpec) -> ExperimentProtocol:
    return ExperimentProtocol(
        research_question="A1 reference mechanics.",
        baseline_model_spec_hash=spec.model_spec_hash,
        intervention_ids=("noop-a", "noop-b"),
        agency_levels=(AgencyLevel.A1,),
        parameter_ranges={"arrival_rate": ParameterRange(low=0.6, high=0.9)},
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
            max_replications=4,
            target_ci_half_width=0.1,
        ),
        warmup=5.0,
        horizon=80.0,
        falsification=FalsificationRule(primary_metric="lead_time", theta_fraction=0.5),
    )


def _world(*, trigger: float, multiplier: float, adaptation_time: float) -> tuple[ExperimentProtocol, SyntheticWorldSpec]:
    policy = A1Policy(
        name=A1PolicyName.BATCHING,
        parameters={
            "backlog_trigger": trigger,
            "service_multiplier": multiplier,
            "adaptation_time": adaptation_time,
            "adaptation_cost_rate": 2.0,
        },
    )
    spec = ModelSpec(
        parameters={
            "arrival_rate": 0.95,
            "service_capacity": 1.0,
            "service_cv": 0.6,
            "rework_probability": 0.15,
        },
        parameter_evidence={
            name: EvidenceClass.ASSUMED
            for name in (
                "arrival_rate",
                "service_capacity",
                "service_cv",
                "rework_probability",
            )
        },
        demand={"kind": "synthetic_open_system"},
        costs={"operating_cost": 1.0},
        agency=AgencySpec(level=AgencyLevel.A1, a1_policy=policy),
    )
    protocol = _protocol(spec)
    world = SyntheticWorldSpec(
        design_index=0,
        arm_id="baseline",
        agency_level=AgencyLevel.A1,
        crn_group="design:0",
        protocol_hash=protocol.protocol_hash,
        baseline_model_spec_hash=spec.model_spec_hash,
        exogenous_parameters={"arrival_rate": 0.95},
        model_spec=spec,
        model_spec_hash=spec.model_spec_hash,
    )
    return protocol, world


def test_a1_policy_parameters_are_typed_and_bounded() -> None:
    params = A1AdaptivePolicyParameters.from_policy(
        A1Policy(
            name=A1PolicyName.BATCHING,
            parameters={
                "backlog_trigger": 2.0,
                "service_multiplier": 0.75,
                "adaptation_time": 0.1,
                "adaptation_cost_rate": 3.0,
            },
        )
    )
    assert params.backlog_trigger == 2.0
    assert params.service_multiplier == 0.75
    assert params.adaptation_time == 0.1
    assert params.adaptation_cost_rate == 3.0


def test_a1_reference_run_is_deterministic_and_emits_adaptation_ledger() -> None:
    protocol, world = _world(trigger=0.0, multiplier=0.75, adaptation_time=0.1)

    left = run_a1_reference_world(
        protocol=protocol,
        world=world,
        root_seed=20261007,
        replication=0,
    )
    right = run_a1_reference_world(
        protocol=protocol,
        world=world,
        root_seed=20261007,
        replication=0,
    )

    assert left == right
    assert left.adaptation_count > 0
    assert left.adaptation_actor_time > 0.0
    assert left.adaptation_cost > 0.0
    assert isclose(
        left.actor_ledger.duration_by_category()[ActorCategory.ADAPTATION],
        left.adaptation_actor_time,
        rel_tol=0.0,
        abs_tol=1e-12,
    )
    left.actor_ledger.assert_complete(start=0.0, end=left.actor_observed_until)
    assert all(
        isclose(
            item.lead_time,
            item.queue_time + item.processing_time + item.rework_time,
            rel_tol=0.0,
            abs_tol=1e-12,
        )
        for item in left.items
    )
    assert left.adaptation_actor_time_measurement <= left.adaptation_actor_time
    assert isclose(
        left.adaptation_cost,
        left.adaptation_actor_time_measurement * 2.0,
        rel_tol=0.0,
        abs_tol=1e-12,
    )


def test_a1_crn_latents_do_not_depend_on_policy_activation() -> None:
    protocol, active = _world(trigger=0.0, multiplier=0.70, adaptation_time=0.0)
    _, inactive = _world(trigger=1e9, multiplier=0.70, adaptation_time=0.0)

    adapted = run_a1_reference_world(
        protocol=protocol,
        world=active,
        root_seed=77,
        replication=1,
    )
    unadapted = run_a1_reference_world(
        protocol=protocol,
        world=inactive.model_copy(
            update={
                "protocol_hash": protocol.protocol_hash,
                "baseline_model_spec_hash": active.baseline_model_spec_hash,
            }
        ),
        root_seed=77,
        replication=1,
    )

    count = min(len(adapted.items), len(unadapted.items))
    assert count > 5
    assert [item.arrival_at for item in adapted.items[:count]] == [
        item.arrival_at for item in unadapted.items[:count]
    ]
    assert [item.service_latent_normal for item in adapted.items[:count]] == [
        item.service_latent_normal for item in unadapted.items[:count]
    ]
    assert [item.rework_latent_uniform for item in adapted.items[:count]] == [
        item.rework_latent_uniform for item in unadapted.items[:count]
    ]


def test_a1_adaptation_can_reduce_delay_without_rewriting_structure() -> None:
    protocol, active = _world(trigger=0.0, multiplier=0.65, adaptation_time=0.0)
    _, inactive = _world(trigger=1e9, multiplier=0.65, adaptation_time=0.0)

    adapted = run_a1_reference_world(
        protocol=protocol,
        world=active,
        root_seed=123,
        replication=0,
    )
    unadapted = run_a1_reference_world(
        protocol=protocol,
        world=inactive.model_copy(
            update={
                "protocol_hash": protocol.protocol_hash,
                "baseline_model_spec_hash": active.baseline_model_spec_hash,
            }
        ),
        root_seed=123,
        replication=0,
    )

    assert adapted.model_spec_hash == active.model_spec_hash
    assert adapted.mean_lead_time < unadapted.mean_lead_time
    assert adapted.adaptation_count > unadapted.adaptation_count


def test_intervention_transition_downtime_is_unavailable_not_idle() -> None:
    protocol, world = _world(trigger=1e9, multiplier=0.75, adaptation_time=0.0)
    transitioned = world.model_copy(update={"intervention_transition_time": 3.0})

    run = run_a1_reference_world(
        protocol=protocol,
        world=transitioned,
        root_seed=99,
        replication=0,
    )

    assert run.actor_ledger.intervals[0].category is ActorCategory.UNAVAILABLE
    assert run.actor_ledger.intervals[0].start == 0.0
    assert run.actor_ledger.intervals[0].end == 3.0
    assert run.actor_ledger.unavailable_time == 3.0

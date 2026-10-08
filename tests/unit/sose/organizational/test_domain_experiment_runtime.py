from __future__ import annotations

from sose.organizational.agency import AgencyLevel
from sose.organizational.domain_experiment_runtime import (
    DomainExperimentPlan,
    run_domain_experiment,
)
from sose.organizational.experiment import ReplicationPlan
from sose.organizational.synthetic_queue_reference import (
    QueueReferenceDomain,
    build_queue_reference_plan_v1,
)


def _small_plan() -> tuple[QueueReferenceDomain, DomainExperimentPlan]:
    reference = QueueReferenceDomain()
    base = build_queue_reference_plan_v1(reference)
    protocol = base.protocol.model_copy(
        update={
            "sample_size": 2,
            "replication_plan": ReplicationPlan(
                min_replications=2,
                max_replications=2,
                target_ci_half_width=0.1,
            ),
            "warmup": 5.0,
            "horizon": 80.0,
        }
    )
    return reference, base.model_copy(update={"protocol": protocol})


def test_generic_runtime_executes_reference_without_domain_special_cases() -> None:
    reference, plan = _small_plan()

    result = run_domain_experiment(reference=reference, plan=plan)

    assert len(result.worlds) == 2 * 3 * 2
    assert len(result.runs) == len(result.worlds) * 2
    assert len(result.references) == len(result.worlds)
    assert result.manifest.world_count == len(result.worlds)
    assert result.manifest.run_count == len(result.runs)
    assert result.manifest.protocol_hash == plan.protocol.protocol_hash
    assert result.manifest.domain_reference_hash == reference.descriptor.descriptor_hash
    assert result.manifest.result_hash == result.result_hash


def test_runtime_preserves_framework_crn_group_across_arms_and_agency() -> None:
    reference, plan = _small_plan()
    result = run_domain_experiment(reference=reference, plan=plan)

    for design_index in range(plan.protocol.sample_size):
        worlds = [
            world for world in result.worlds
            if world.design_index == design_index
        ]
        assert len({world.crn_group for world in worlds}) == 1
        assert {world.agency_configuration.level for world in worlds} == {
            AgencyLevel.A0,
            AgencyLevel.A1,
        }


def test_runtime_collects_standard_observations_and_typed_reference_evidence() -> None:
    reference, plan = _small_plan()
    result = run_domain_experiment(reference=reference, plan=plan)

    assert all("mean_lead_time" in run.observation.metrics for run in result.runs)
    assert all("throughput" in run.observation.metrics for run in result.runs)
    assert any(record.ground_truth for record in result.references)
    assert {record.regime.label for record in result.references}
    assert any(assessment.eligible for assessment in result.assessments)


def test_runtime_result_is_reproducible_and_hash_addressed() -> None:
    reference, plan = _small_plan()

    left = run_domain_experiment(reference=reference, plan=plan)
    right = run_domain_experiment(reference=reference, plan=plan)

    assert left.result_hash == right.result_hash
    assert left.manifest == right.manifest
    assert [run.evidence_hash for run in left.runs] == [
        run.evidence_hash for run in right.runs
    ]


def test_runtime_rejects_non_fixed_replication_plan_at_gate_a() -> None:
    reference, plan = _small_plan()
    protocol = plan.protocol.model_copy(
        update={
            "replication_plan": ReplicationPlan(
                min_replications=2,
                max_replications=3,
                target_ci_half_width=0.1,
            )
        }
    )

    try:
        run_domain_experiment(
            reference=reference,
            plan=plan.model_copy(update={"protocol": protocol}),
        )
    except ValueError as exc:
        assert "fixed replication" in str(exc)
    else:
        raise AssertionError("Gate-A runtime must reject adaptive replication plans")


def test_a0_and_a1_worlds_share_structural_model_but_not_world_identity() -> None:
    reference, plan = _small_plan()
    result = run_domain_experiment(reference=reference, plan=plan)

    a0 = next(
        world for world in result.worlds
        if world.design_index == 0
        and world.arm_id == "baseline"
        and world.agency_configuration.level is AgencyLevel.A0
    )
    a1 = next(
        world for world in result.worlds
        if world.design_index == 0
        and world.arm_id == "baseline"
        and world.agency_configuration.level is AgencyLevel.A1
    )

    assert a0.structural_configuration_hash == a1.structural_configuration_hash
    assert a0.world_hash != a1.world_hash

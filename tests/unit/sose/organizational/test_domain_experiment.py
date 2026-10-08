from __future__ import annotations

from collections import defaultdict

import pytest

from sose.organizational.agency import AgencyLevel
from sose.organizational.domain_experiment import (
    AgencyConfiguration,
    DomainExperimentPlan,
    build_domain_experiment_worlds,
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


def test_framework_owns_world_expansion_and_crn_groups() -> None:
    reference, plan = _small_plan()

    worlds = build_domain_experiment_worlds(reference=reference, plan=plan)

    assert len(worlds) == 2 * 3 * 2
    grouped: dict[int, list] = defaultdict(list)
    for world in worlds:
        grouped[world.design_index].append(world)

    assert set(grouped) == {0, 1}
    for design_index, items in grouped.items():
        assert {item.crn_group for item in items} == {f"design:{design_index}"}
        assert {item.arm_id for item in items} == {
            "baseline",
            "capacity-up",
            "automation-rework",
        }
        assert {item.agency_level for item in items} == {
            AgencyLevel.A0,
            AgencyLevel.A1,
        }


def test_world_identity_contains_configuration_not_realized_outputs() -> None:
    reference, plan = _small_plan()
    world = build_domain_experiment_worlds(reference=reference, plan=plan)[0]

    payload = world.canonical_payload()

    assert "exogenous_parameters" in payload
    assert "model_spec_hash" in payload
    assert "agency_configuration" in payload
    assert "lead_time" not in payload
    assert "throughput" not in payload
    assert "mean_wip" not in payload
    assert "utilization" not in payload


def test_framework_rejects_parameter_range_outside_domain_capability() -> None:
    reference, plan = _small_plan()
    payload = plan.protocol.model_dump(mode="python")
    ranges = dict(payload["parameter_ranges"])
    ranges["arrival_rate"] = ranges["arrival_rate"].model_copy(
        update={"high": 99.0}
    )
    payload["parameter_ranges"] = ranges
    protocol = plan.protocol.__class__.model_validate(payload)

    with pytest.raises(ValueError, match="outside domain-declared range"):
        build_domain_experiment_worlds(
            reference=reference,
            plan=plan.model_copy(update={"protocol": protocol}),
        )


def test_framework_rejects_unsupported_agency_configuration_before_execution() -> None:
    reference, plan = _small_plan()
    bad = AgencyConfiguration(
        level=AgencyLevel.A1,
        capability_id="batching",
        parameters={
            "backlog_trigger": 2.0,
            "service_multiplier": 2.0,
            "adaptation_time": 0.05,
            "adaptation_cost_rate": 0.15,
        },
    )

    with pytest.raises(ValueError, match="outside capability range"):
        build_domain_experiment_worlds(
            reference=reference,
            plan=plan.model_copy(
                update={
                    "agency_configurations": (
                        plan.agency_configurations[0],
                        bad,
                    )
                }
            ),
        )


def test_generic_runner_collects_observations_claims_regimes_and_manifest() -> None:
    reference, plan = _small_plan()

    result = run_domain_experiment(reference=reference, plan=plan)

    assert len(result.worlds) == 12
    assert len(result.runs) == 24
    assert len(result.references) == 12
    assert result.manifest.world_count == 12
    assert result.manifest.run_count == 24
    assert result.manifest.protocol_hash == plan.protocol.protocol_hash
    assert result.manifest.domain_reference_hash == reference.descriptor.descriptor_hash
    assert result.manifest.result_hash == result.result_hash
    assert all(run.observation.metrics for run in result.runs)
    assert any(reference_record.ground_truth for reference_record in result.references)
    assert any(assessment.eligible for assessment in result.assessments)


def test_generic_result_is_reproducible() -> None:
    reference, plan = _small_plan()

    left = run_domain_experiment(reference=reference, plan=plan)
    right = run_domain_experiment(reference=reference, plan=plan)

    assert left.result_hash == right.result_hash
    assert left.manifest == right.manifest
    assert [run.evidence_hash for run in left.runs] == [
        run.evidence_hash for run in right.runs
    ]


def test_plan_requires_one_configuration_for_each_protocol_agency_level() -> None:
    reference = QueueReferenceDomain()
    base = build_queue_reference_plan_v1(reference)

    with pytest.raises(ValueError, match="exactly one agency configuration"):
        DomainExperimentPlan(
            protocol=base.protocol,
            design_seed=base.design_seed,
            root_seed=base.root_seed,
            agency_configurations=(base.agency_configurations[0],),
        )

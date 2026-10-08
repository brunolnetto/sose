from __future__ import annotations

from math import isclose

from sose.organizational.agency import AgencyLevel
from sose.organizational.domain_experiment_conformance import (
    run_domain_experiment_conformance,
)
from sose.organizational.domain_experiment_runtime import run_domain_experiment
from sose.organizational.manufacturing_reference import (
    ManufacturingReferenceDomain,
    build_manufacturing_preflight_plan_v1,
)


def test_manufacturing_reference_descriptor_is_a0_quantity_only() -> None:
    reference = ManufacturingReferenceDomain()
    descriptor = reference.descriptor

    assert descriptor.identity.domain == "manufacturing"
    assert descriptor.identity.process_manifest_domain == "manufacturing"
    assert descriptor.identity.specification_path == "docs/examples/manufacturing/specification.md"
    assert [(item.name, item.default, item.range.low, item.range.high) for item in descriptor.parameters] == [
        ("quantity", 10.0, 1.0, 1000.0)
    ]
    assert [(item.level, item.capability_id) for item in descriptor.agency_capabilities] == [
        (AgencyLevel.A0, "fixed")
    ]


def test_manufacturing_interventions_bind_to_model_spec_mechanics() -> None:
    reference = ManufacturingReferenceDomain()
    baseline = reference.build_model({"quantity": 10.0})
    interventions = {item.intervention_id: item for item in reference.interventions()}

    assert set(interventions) == {"machine_downtime", "yield_degradation"}
    downtime, _ = interventions["machine_downtime"].apply(baseline)
    degraded, _ = interventions["yield_degradation"].apply(baseline)

    assert baseline.parameters["quantity"] == 10.0
    assert baseline.parameters["yield_factor"] == 1.0
    assert baseline.parameters["machine_downtime_hours"] == 0.0
    assert downtime.parameters["machine_downtime_hours"] == 3.0
    assert downtime.parameters["yield_factor"] == 1.0
    assert degraded.parameters["yield_factor"] == 0.8
    assert degraded.parameters["machine_downtime_hours"] == 0.0


def test_manufacturing_preflight_executes_all_arms_and_preserves_expected_mechanics() -> None:
    reference = ManufacturingReferenceDomain()
    result = run_domain_experiment(
        reference=reference,
        plan=build_manufacturing_preflight_plan_v1(reference),
    )

    assert len(result.worlds) == 2 * 3
    assert len(result.runs) == 2 * 3 * 2

    by_key = {
        (run.design_index, run.arm_id, run.replication): run
        for run in result.runs
    }
    evidence = {
        (record.world_hash, record.replication): record.evidence
        for record in result.evidence
    }
    world_by_key = {
        (world.design_index, world.arm_id): world
        for world in result.worlds
    }

    for design_index in range(2):
        for replication in range(2):
            nominal = by_key[(design_index, "baseline", replication)]
            yield_run = by_key[(design_index, "yield_degradation", replication)]
            downtime = by_key[(design_index, "machine_downtime", replication)]

            assert nominal.observation.metrics["completed"] == 1.0
            assert yield_run.observation.metrics["completed"] == 1.0
            assert downtime.observation.metrics["completed"] == 1.0

            nominal_output = nominal.observation.metrics["output_quantity"]
            yield_output = yield_run.observation.metrics["output_quantity"]
            assert isclose(yield_output, nominal_output * 0.8, rel_tol=0.0, abs_tol=1e-9)
            assert yield_run.observation.metrics["yield_ratio"] == 0.8

            assert nominal.observation.metrics["breakdown_count"] == 0.0
            assert yield_run.observation.metrics["breakdown_count"] == 0.0
            assert nominal.observation.metrics["machine_reacquired"] == 0.0
            assert yield_run.observation.metrics["machine_reacquired"] == 0.0
            assert downtime.observation.metrics["breakdown_count"] == 1.0
            assert downtime.observation.metrics["preemption_count"] == 1.0
            assert downtime.observation.metrics["machine_reacquired"] == 1.0
            assert downtime.observation.metrics["material_issue_count"] == 1.0

            downtime_world = world_by_key[(design_index, "machine_downtime")]
            downtime_evidence = evidence[(downtime_world.world_hash, replication)]
            assert downtime_evidence.order_state == "completed"
            assert downtime_evidence.operation_state == "done"
            assert downtime_evidence.machine_reacquired is True
            assert downtime_evidence.material_issue_count == 1


def test_manufacturing_preflight_is_deterministic_and_passes_generic_conformance() -> None:
    reference = ManufacturingReferenceDomain()
    plan = build_manufacturing_preflight_plan_v1(reference)

    left = run_domain_experiment_conformance(reference=reference, plan=plan)
    right = run_domain_experiment_conformance(reference=reference, plan=plan)

    assert left.passed
    assert all(check.passed for check in left.checks)
    assert left == right
    assert left.report_hash == right.report_hash


def test_manufacturing_ground_truth_is_configuration_only_and_typed() -> None:
    reference = ManufacturingReferenceDomain()
    result = run_domain_experiment(
        reference=reference,
        plan=build_manufacturing_preflight_plan_v1(reference),
    )

    by_world = {record.world_hash: record for record in result.references}
    for world in result.worlds:
        record = by_world[world.world_hash]
        claim_ids = {claim.claim_id for claim in record.ground_truth}
        assert {
            "mfg.completed",
            "mfg.material-issued-once",
            "mfg.raw-material-consumed",
            "mfg.wip-released",
            "mfg.yield-ratio",
            "mfg.breakdown-count",
        } <= claim_ids

        yield_claim = next(
            claim for claim in record.ground_truth if claim.claim_id == "mfg.yield-ratio"
        )
        assert yield_claim.expected == float(world.model_spec.parameters["yield_factor"])

        breakdown_claim = next(
            claim for claim in record.ground_truth if claim.claim_id == "mfg.breakdown-count"
        )
        expected_breakdowns = 1.0 if float(world.model_spec.parameters["machine_downtime_hours"]) > 0 else 0.0
        assert breakdown_claim.expected == expected_breakdowns


def test_manufacturing_has_no_demand_surge_or_quality_hold_experiment_arm() -> None:
    reference = ManufacturingReferenceDomain()
    plan = build_manufacturing_preflight_plan_v1(reference)

    assert plan.protocol.intervention_ids == ("machine_downtime", "yield_degradation")
    assert set(plan.protocol.parameter_ranges) == {"quantity"}
    assert plan.protocol.agency_levels == (AgencyLevel.A0,)



def test_manufacturing_rejects_protocol_horizon_shorter_than_configured_downtime() -> None:
    reference = ManufacturingReferenceDomain()
    plan = build_manufacturing_preflight_plan_v1(reference)
    short_protocol = plan.protocol.model_copy(update={"horizon": 2.0})
    short_plan = plan.model_copy(update={"protocol": short_protocol})

    import pytest

    with pytest.raises(ValueError, match="horizon"):
        run_domain_experiment(reference=reference, plan=short_plan)

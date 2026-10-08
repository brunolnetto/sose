from __future__ import annotations

from sose.organizational.agency import AgencyLevel
from sose.organizational.experiment import ExperimentProtocol, SamplingDesign
from sose.organizational.manufacturing_protocol import (
    MANUFACTURING_V1_DESIGN_SEED,
    MANUFACTURING_V1_ROOT_SEED,
    build_manufacturing_official_plan_v1,
    build_manufacturing_protocol_v1,
)
from sose.organizational.manufacturing_reference import ManufacturingReferenceDomain


def test_manufacturing_v1_protocol_is_real_framework_schema_and_binds_adapter() -> None:
    reference = ManufacturingReferenceDomain()
    protocol = build_manufacturing_protocol_v1(reference)

    assert isinstance(protocol, ExperimentProtocol)
    assert protocol.protocol_version == "1"
    assert protocol.baseline_model_spec_hash == reference.build_model({"quantity": 10.0}).model_spec_hash
    assert protocol.intervention_ids == ("machine_downtime", "yield_degradation")
    assert protocol.agency_levels == (AgencyLevel.A0,)
    assert set(protocol.parameter_ranges) == {"quantity"}
    assert protocol.parameter_ranges["quantity"].low == 1.0
    assert protocol.parameter_ranges["quantity"].high == 1000.0
    assert protocol.sampling_design is SamplingDesign.LATIN_HYPERCUBE
    assert protocol.sample_size == 6
    assert protocol.replication_plan.min_replications == 2
    assert protocol.replication_plan.max_replications == 2
    assert protocol.warmup == 0.0
    assert protocol.horizon == 24.0

    reparsed = ExperimentProtocol.model_validate(protocol.canonical_payload())
    assert reparsed == protocol
    assert reparsed.protocol_hash == protocol.protocol_hash


def test_manufacturing_v1_protocol_preregisters_only_finite_adapter_metrics() -> None:
    protocol = build_manufacturing_protocol_v1()
    names = tuple(metric.name for metric in protocol.outcomes)

    assert names == (
        "completed",
        "lead_time_seconds",
        "output_quantity",
        "yield_ratio",
        "transition_count",
        "rework_count",
        "breakdown_count",
    )
    assert protocol.falsification.primary_metric == "output_quantity"


def test_manufacturing_v1_official_plan_freezes_seed_and_a0_configuration() -> None:
    plan = build_manufacturing_official_plan_v1()

    assert plan.protocol == build_manufacturing_protocol_v1()
    assert plan.design_seed == MANUFACTURING_V1_DESIGN_SEED == 20261008
    assert plan.root_seed == MANUFACTURING_V1_ROOT_SEED == 20261008
    assert len(plan.agency_configurations) == 1
    configuration = plan.agency_configurations[0]
    assert configuration.level is AgencyLevel.A0
    assert configuration.capability_id == "fixed"
    assert configuration.parameters == {}


def test_manufacturing_v1_official_plan_is_hash_stable() -> None:
    left = build_manufacturing_official_plan_v1()
    right = build_manufacturing_official_plan_v1()

    assert left == right
    assert left.plan_hash == right.plan_hash
    assert left.protocol.protocol_hash == right.protocol.protocol_hash
    assert len(left.plan_hash) == 64
    assert len(left.protocol.protocol_hash) == 64

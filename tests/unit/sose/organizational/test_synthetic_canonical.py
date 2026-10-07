from __future__ import annotations

from sose.organizational.agency import AgencyLevel
from sose.organizational.model_spec import InterventionClass
from sose.organizational.synthetic_analysis import analytical_world_reference
from sose.organizational.synthetic_canonical import (
    CANONICAL_DESIGN_SEED,
    CANONICAL_ROOT_SEED,
    build_reference_synthetic_canonical,
    canonical_verification_criteria,
)
from sose.organizational.synthetic_study import generate_synthetic_worlds


def test_reference_canonical_freezes_exogenous_doe_and_intervention_classes() -> None:
    baseline, protocol, interventions, agency_specs = build_reference_synthetic_canonical()

    assert protocol.baseline_model_spec_hash == baseline.model_spec_hash
    assert protocol.sample_size == 24
    assert protocol.replication_plan.min_replications == 4
    assert protocol.replication_plan.max_replications == 4
    assert protocol.agency_levels == (AgencyLevel.A0,)
    assert set(protocol.parameter_ranges) == {
        "arrival_rate",
        "service_capacity",
        "service_cv",
        "rework_probability",
        "transition_cost",
    }
    assert {item.intervention_class for item in interventions} == {
        InterventionClass.CAPACITY,
        InterventionClass.POLICY,
        InterventionClass.AUTOMATION,
        InterventionClass.STRUCTURAL,
    }
    assert set(item.intervention_id for item in interventions) == set(protocol.intervention_ids)
    assert set(agency_specs) == {AgencyLevel.A0}
    assert CANONICAL_DESIGN_SEED == 20261007
    assert CANONICAL_ROOT_SEED == 20261008


def test_reference_canonical_crosses_the_mechanical_saturation_boundary() -> None:
    baseline, protocol, interventions, agency_specs = build_reference_synthetic_canonical()
    worlds = generate_synthetic_worlds(
        protocol=protocol,
        baseline=baseline,
        interventions=interventions,
        agency_specs=agency_specs,
        seed=CANONICAL_DESIGN_SEED,
    )
    baseline_refs = [
        analytical_world_reference(world)
        for world in worlds
        if world.arm_id == "baseline"
    ]

    assert len(worlds) == protocol.sample_size * (1 + len(interventions))
    assert any(reference.stable for reference in baseline_refs)
    assert any(not reference.stable for reference in baseline_refs)


def test_reference_canonical_verification_criteria_are_frozen_before_execution() -> None:
    criteria = canonical_verification_criteria()

    assert criteria.max_mean_relative_error_stable == 0.25
    assert criteria.min_direction_recovery_rate == 0.85
    assert criteria.require_stable_worlds is True
    assert criteria.require_saturated_worlds is True

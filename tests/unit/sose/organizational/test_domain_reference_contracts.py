from __future__ import annotations

from sose.organizational.agency import AgencyLevel
from sose.organizational.domain_reference import (
    DomainReference,
    GroundTruthKind,
    GroundTruthTargetKind,
)
from sose.organizational.queue_reference_adapter import QueueReferenceAdapter
from sose.organizational.synthetic_a1_experiment import build_a0_a1_reference_design_v1
from sose.organizational.synthetic_reference_experiment import build_reference_synthetic_experiment_v1


def test_queue_reference_declares_exogenous_parameter_space_without_world_ownership() -> None:
    adapter = QueueReferenceAdapter()
    design = build_reference_synthetic_experiment_v1()

    space = adapter.parameter_space()
    definitions = {item.name: item for item in space.parameters}

    assert set(definitions) == set(design.protocol.parameter_ranges)
    assert {
        name: (definition.range.low, definition.range.high)
        for name, definition in definitions.items()
    } == {
        name: (bounds.low, bounds.high)
        for name, bounds in design.protocol.parameter_ranges.items()
    }
    assert not hasattr(adapter, "build_worlds")
    assert isinstance(adapter, DomainReference)


def test_queue_reference_declares_typed_a0_a1_capabilities() -> None:
    adapter = QueueReferenceAdapter()

    capabilities = {item.level: item for item in adapter.agency_capabilities()}

    assert set(capabilities) == {AgencyLevel.A0, AgencyLevel.A1}
    assert capabilities[AgencyLevel.A0].capability_id == "fixed-policy"
    assert dict(capabilities[AgencyLevel.A0].parameter_ranges) == {}

    a1 = capabilities[AgencyLevel.A1]
    assert a1.capability_id == "batching"
    assert set(a1.parameter_ranges) == {
        "backlog_trigger",
        "service_multiplier",
        "adaptation_time",
        "adaptation_cost_rate",
    }
    assert a1.parameter_ranges["service_multiplier"].low > 0.0
    assert a1.parameter_ranges["service_multiplier"].high == 1.0
    assert a1.defaults["service_multiplier"] == 0.70


def test_queue_reference_descriptor_is_hash_stable_and_matches_capabilities() -> None:
    left = QueueReferenceAdapter()
    right = QueueReferenceAdapter()

    assert left.identity == right.identity
    assert left.identity.reference_hash == right.identity.reference_hash
    assert left.descriptor().descriptor_hash == right.descriptor().descriptor_hash
    assert left.descriptor().identity == left.identity
    assert {item.level for item in left.descriptor().agency_capabilities} == {
        AgencyLevel.A0,
        AgencyLevel.A1,
    }


def test_a0_ground_truth_separates_mechanistic_regime_from_analytical_metric() -> None:
    adapter = QueueReferenceAdapter()
    design = build_reference_synthetic_experiment_v1()
    stable = next(
        world
        for world in design.worlds
        if world.arm_id == "baseline" and adapter.classify_regime(world).stable
    )
    saturated = next(
        world
        for world in design.worlds
        if world.arm_id == "baseline" and not adapter.classify_regime(world).stable
    )

    stable_claims = {claim.claim_id: claim for claim in adapter.ground_truth(stable)}
    saturated_claims = {
        claim.claim_id: claim for claim in adapter.ground_truth(saturated)
    }

    regime = stable_claims["queue.regime"]
    assert regime.kind is GroundTruthKind.MECHANISTIC
    assert regime.target_kind is GroundTruthTargetKind.REGIME
    assert regime.eligible is True

    lead = stable_claims["queue.mean_lead_time"]
    assert lead.kind is GroundTruthKind.ANALYTICAL
    assert lead.target_kind is GroundTruthTargetKind.METRIC
    assert lead.eligible is True
    assert isinstance(lead.expected, float)

    saturated_lead = saturated_claims["queue.mean_lead_time"]
    assert saturated_lead.kind is GroundTruthKind.ANALYTICAL
    assert saturated_lead.eligible is False
    assert saturated_lead.expected is None
    assert saturated_lead.ineligibility_reason is not None
    assert "stationary" in saturated_lead.ineligibility_reason.lower()


def test_a1_ground_truth_uses_mechanistic_policy_regime_without_fake_analytical_truth() -> None:
    adapter = QueueReferenceAdapter()
    design = build_a0_a1_reference_design_v1()
    world = next(
        item
        for item in design.worlds
        if item.agency_level is AgencyLevel.A1 and item.arm_id == "baseline"
    )

    claims = adapter.ground_truth(world)

    assert {claim.kind for claim in claims} == {GroundTruthKind.MECHANISTIC}
    assert {claim.claim_id for claim in claims} == {"queue.a1_regime"}
    assert claims[0].eligible is True


def test_adapter_execution_preserves_existing_a0_a1_runners_and_standard_observation() -> None:
    adapter = QueueReferenceAdapter()
    design = build_a0_a1_reference_design_v1()
    a0 = next(
        world
        for world in design.worlds
        if world.design_index == 0
        and world.arm_id == "baseline"
        and world.agency_level is AgencyLevel.A0
    )
    a1 = next(
        world
        for world in design.worlds
        if world.design_index == 0
        and world.arm_id == "baseline"
        and world.agency_level is AgencyLevel.A1
    )

    a0_result = adapter.execute(
        protocol=design.protocol,
        world=a0,
        root_seed=design.root_seed,
        replication=0,
    )
    a1_result = adapter.execute(
        protocol=design.protocol,
        world=a1,
        root_seed=design.root_seed,
        replication=0,
    )

    a0_observation = adapter.observation(a0_result)
    a1_observation = adapter.observation(a1_result)

    assert a0_observation.world_hash == a0.world_hash
    assert a1_observation.world_hash == a1.world_hash
    assert a0_observation.replication == 0
    assert a1_observation.replication == 0
    assert a0_observation.metrics["mean_lead_time"] == a0_result.mean_lead_time
    assert a1_observation.metrics["mean_lead_time"] == a1_result.mean_lead_time
    assert "adaptation_actor_time" not in a0_observation.metrics
    assert (
        a1_observation.metrics["adaptation_actor_time"]
        == a1_result.adaptation_actor_time_measurement
    )


def test_queue_adapter_ground_truth_is_hash_stable() -> None:
    left = QueueReferenceAdapter()
    right = QueueReferenceAdapter()
    design = build_reference_synthetic_experiment_v1()
    world = design.worlds[0]

    left_claims = left.ground_truth(world)
    right_claims = right.ground_truth(world)

    assert left_claims == right_claims
    assert [claim.claim_hash for claim in left_claims] == [
        claim.claim_hash for claim in right_claims
    ]

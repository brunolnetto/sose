from __future__ import annotations

import pytest
from pydantic import ValidationError

from sose.organizational.agency import AgencyLevel
from sose.organizational.domain_reference import (
    AgencyCapabilitySpec,
    DomainReferenceDescriptor,
    DomainReferenceIdentity,
    GroundTruthClaim,
    GroundTruthKind,
    GroundTruthTargetKind,
    ParameterDefinition,
)
from sose.organizational.experiment import ParameterRange
from sose.organizational.model_spec import EvidenceClass


def _identity() -> DomainReferenceIdentity:
    return DomainReferenceIdentity(
        domain="manufacturing",
        reference_id="manufacturing-reference",
        reference_version="1",
        process_manifest_domain="manufacturing",
        specification_path="docs/examples/manufacturing/specification.md",
    )


def _arrival() -> ParameterDefinition:
    return ParameterDefinition(
        name="arrival_rate",
        description="External arrival intensity.",
        units="items/hour",
        default=0.8,
        range=ParameterRange(low=0.2, high=1.4),
        evidence_class=EvidenceClass.ASSUMED,
    )


def _capacity() -> ParameterDefinition:
    return ParameterDefinition(
        name="service_capacity",
        description="Configured nominal service capacity.",
        units="items/hour",
        default=1.0,
        range=ParameterRange(low=0.5, high=2.0),
        evidence_class=EvidenceClass.ASSUMED,
    )


def _a0() -> AgencyCapabilitySpec:
    return AgencyCapabilitySpec(
        level=AgencyLevel.A0,
        capability_id="fixed-policy",
    )


def _a1() -> AgencyCapabilitySpec:
    return AgencyCapabilitySpec(
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
        compatibility_constraints=(
            "does not change nominal resource capacity",
            "does not change demand generation",
        ),
    )


def test_domain_reference_identity_is_hash_addressed_and_rejects_unsafe_paths() -> None:
    identity = _identity()

    assert len(identity.reference_hash) == 64
    assert identity.reference_hash == _identity().reference_hash

    with pytest.raises(ValidationError, match="repository-relative"):
        DomainReferenceIdentity(
            domain="manufacturing",
            reference_id="manufacturing-reference",
            reference_version="1",
            process_manifest_domain="manufacturing",
            specification_path="../manufacturing/specification.md",
        )


def test_parameter_definition_requires_default_inside_declared_exogenous_range() -> None:
    assert _arrival().default == 0.8

    with pytest.raises(ValidationError, match="default must lie within"):
        ParameterDefinition(
            name="arrival_rate",
            description="External arrival intensity.",
            default=2.0,
            range=ParameterRange(low=0.2, high=1.4),
            evidence_class=EvidenceClass.ASSUMED,
        )


def test_agency_capability_exposes_domain_owned_configuration_contract() -> None:
    capability = _a1()

    assert capability.level is AgencyLevel.A1
    assert capability.capability_id == "adaptive-batching"
    assert set(capability.parameter_ranges) == {
        "backlog_trigger",
        "service_multiplier",
    }
    assert dict(capability.defaults) == {
        "backlog_trigger": 2.0,
        "service_multiplier": 0.7,
    }

    with pytest.raises(ValidationError, match="defaults must exactly match"):
        AgencyCapabilitySpec(
            level=AgencyLevel.A1,
            capability_id="bad-policy",
            parameter_ranges={
                "threshold": ParameterRange(low=0.0, high=10.0),
            },
            defaults={},
        )


def test_a2_capability_requires_observation_and_action_contracts_and_a3_is_forbidden() -> None:
    with pytest.raises(ValidationError, match="A2 requires observation and action contracts"):
        AgencyCapabilitySpec(
            level=AgencyLevel.A2,
            capability_id="manager-controller",
        )

    a2 = AgencyCapabilitySpec(
        level=AgencyLevel.A2,
        capability_id="manager-controller",
        observation_contract_ids=("lagged-kpis-v1",),
        action_contract_ids=("capacity-actions-v1",),
    )
    assert a2.observation_contract_ids == ("lagged-kpis-v1",)
    assert a2.action_contract_ids == ("capacity-actions-v1",)

    with pytest.raises(ValidationError, match="A3 is outside"):
        AgencyCapabilitySpec(
            level=AgencyLevel.A3,
            capability_id="institutional-change",
        )


def test_ground_truth_claim_requires_explicit_eligibility_and_provenance() -> None:
    eligible = GroundTruthClaim(
        claim_id="stable-load",
        kind=GroundTruthKind.MECHANISTIC,
        target_kind=GroundTruthTargetKind.REGIME,
        target_name="stability",
        expected={"class": "stable", "rho_upper_bound": 1.0},
        assumptions=("configured rates are exogenous",),
        eligibility_rule="configured offered load < 1",
        eligible=True,
        provenance=("manufacturing/reference.py:classify_regime",),
    )
    same = GroundTruthClaim(
        claim_id="stable-load",
        kind=GroundTruthKind.MECHANISTIC,
        target_kind=GroundTruthTargetKind.REGIME,
        target_name="stability",
        expected={"rho_upper_bound": 1.0, "class": "stable"},
        assumptions=("configured rates are exogenous",),
        eligibility_rule="configured offered load < 1",
        eligible=True,
        provenance=("manufacturing/reference.py:classify_regime",),
    )

    assert eligible.claim_hash == same.claim_hash

    with pytest.raises(ValidationError, match="ineligibility_reason"):
        GroundTruthClaim(
            claim_id="stationary-mean",
            kind=GroundTruthKind.ANALYTICAL,
            target_kind=GroundTruthTargetKind.METRIC,
            target_name="mean_lead_time",
            expected=1.25,
            assumptions=("stationary queue",),
            eligibility_rule="rho < 1",
            eligible=False,
            provenance=("queueing-theory",),
        )


def test_domain_reference_descriptor_is_order_independent_and_rejects_duplicates() -> None:
    left = DomainReferenceDescriptor(
        identity=_identity(),
        parameters=(_arrival(), _capacity()),
        agency_capabilities=(_a0(), _a1()),
        ground_truth_kinds=(
            GroundTruthKind.ANALYTICAL,
            GroundTruthKind.MECHANISTIC,
            GroundTruthKind.INVARIANT,
        ),
    )
    right = DomainReferenceDescriptor(
        identity=_identity(),
        parameters=(_capacity(), _arrival()),
        agency_capabilities=(_a1(), _a0()),
        ground_truth_kinds=(
            GroundTruthKind.INVARIANT,
            GroundTruthKind.MECHANISTIC,
            GroundTruthKind.ANALYTICAL,
        ),
    )

    assert left.descriptor_hash == right.descriptor_hash
    assert left.canonical_payload() == right.canonical_payload()

    with pytest.raises(ValidationError, match="duplicate parameter names"):
        DomainReferenceDescriptor(
            identity=_identity(),
            parameters=(_arrival(), _arrival()),
            agency_capabilities=(_a0(),),
            ground_truth_kinds=(GroundTruthKind.MECHANISTIC,),
        )

    with pytest.raises(ValidationError, match="duplicate agency capability"):
        DomainReferenceDescriptor(
            identity=_identity(),
            parameters=(_arrival(),),
            agency_capabilities=(_a1(), _a1()),
            ground_truth_kinds=(GroundTruthKind.MECHANISTIC,),
        )

from __future__ import annotations

import pytest
from pydantic import ValidationError

from sose.organizational.agency import A1PolicyName, AgencyLevel
from sose.organizational.model_spec import ModelSpec


def test_model_spec_defaults_to_valid_a0_agency() -> None:
    spec = ModelSpec(parameters={}, parameter_evidence={})

    assert spec.agency.level is AgencyLevel.A0
    assert spec.canonical_payload()["agency"] == {
        "level": "A0",
        "a1_policy": None,
        "a2_observation": None,
    }


def test_model_spec_rejects_a3_and_invalid_a1_a2_configuration() -> None:
    with pytest.raises(ValidationError, match="A3 is outside"):
        ModelSpec(agency={"level": "A3"}, parameters={}, parameter_evidence={})

    with pytest.raises(ValidationError, match="A1 requires"):
        ModelSpec(agency={"level": "A1"}, parameters={}, parameter_evidence={})

    with pytest.raises(ValidationError, match="A2 requires"):
        ModelSpec(agency={"level": "A2"}, parameters={}, parameter_evidence={})


def test_model_spec_hash_includes_valid_a1_policy_configuration() -> None:
    baseline = ModelSpec(parameters={}, parameter_evidence={})
    adaptive = ModelSpec(
        agency={
            "level": "A1",
            "a1_policy": {
                "name": A1PolicyName.QUEUE_BYPASS,
                "parameters": {"threshold": 3},
            },
        },
        parameters={},
        parameter_evidence={},
    )

    assert adaptive.agency.level is AgencyLevel.A1
    assert baseline.model_spec_hash != adaptive.model_spec_hash


def test_intervention_may_transform_agency_only_through_valid_contract() -> None:
    from sose.organizational.model_spec import InterventionClass, ModelIntervention

    baseline = ModelSpec(parameters={}, parameter_evidence={})
    valid = ModelIntervention(
        intervention_id="enable-batching",
        intervention_class=InterventionClass.POLICY,
        set_values={
            "/agency": {
                "level": "A1",
                "a1_policy": {"name": "batching", "parameters": {"size": 5}},
                "a2_observation": None,
            }
        },
    )
    transformed, _ = valid.apply(baseline)
    assert transformed.agency.level is AgencyLevel.A1

    invalid = ModelIntervention(
        intervention_id="enable-a3",
        intervention_class=InterventionClass.POLICY,
        set_values={"/agency": {"level": "A3"}},
    )
    with pytest.raises(ValidationError, match="A3 is outside"):
        invalid.apply(baseline)

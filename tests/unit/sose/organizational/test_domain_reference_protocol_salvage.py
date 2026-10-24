"""R1: use the existing experiment runtime authority, not a shadow protocol."""
from __future__ import annotations

from inspect import signature
from types import MappingProxyType

import pytest
from pydantic import ValidationError

from sose.organizational.domain_reference import ParameterDefinition, ParameterSpaceSpec
from sose.organizational.domain_experiment_runtime import (
    DomainReference, DomainExecutionRequest, ExperimentObservation, ExperimentRunRecord,
)
from sose.organizational.experiment import ParameterRange
from sose.organizational.model_spec import EvidenceClass


def _parameter(name: str, default: float = 0.5):
    return ParameterDefinition(
        name=name, description="synthetic exogenous input", units="per_second",
        default=default, range=ParameterRange(low=0.1, high=1.0),
        evidence_class=EvidenceClass.ASSUMED,
    )


def test_parameter_space_is_hash_stable_and_sort_canonical():
    alpha, beta = _parameter("arrival"), _parameter("service", 0.8)
    left = ParameterSpaceSpec(parameters=(beta, alpha))
    right = ParameterSpaceSpec(parameters=(alpha, beta))
    assert left.canonical_payload() == right.canonical_payload()
    assert left.parameter_space_hash == right.parameter_space_hash
    assert tuple(p["name"] for p in left.canonical_payload()["parameters"]) == (
        "arrival", "service",
    )


def test_parameter_space_rejects_empty_duplicate_and_out_of_range():
    with pytest.raises(ValidationError):
        ParameterSpaceSpec(parameters=())
    with pytest.raises(ValueError, match="duplicate parameter"):
        ParameterSpaceSpec(parameters=(_parameter("same"), _parameter("same")))
    with pytest.raises(ValueError, match="within"):
        _parameter("out-of-bounds", 10.0)


def test_observation_uses_runtime_canonical_object_and_run_hash_input():
    observation = ExperimentObservation(metrics={"z": 2.0, "a": 1.0})
    assert isinstance(observation.metrics, MappingProxyType)
    assert observation.canonical_payload() == {"metrics": {"a": 1.0, "z": 2.0}}
    record = ExperimentRunRecord(
        world_hash="world:1", design_index=0, arm_id="baseline",
        agency_level="a0", replication=0, replication_seed=1,
        evidence_hash="1" * 64, observation=observation,
    )
    assert record.observation is observation
    assert record.canonical_payload()["observation"] == observation.canonical_payload()
    with pytest.raises(TypeError):
        observation.metrics["a"] = 2.0


@pytest.mark.parametrize("value", [float("nan"), float("inf"), -float("inf")])
def test_runtime_observation_rejects_nonfinite_values(value):
    with pytest.raises(ValidationError):
        ExperimentObservation(metrics={"late": value})


def test_reference_execute_signature_accepts_runtime_request_object():
    parameters = tuple(signature(DomainReference.execute).parameters)
    assert parameters == ("self", "request")
    assert "DomainExecutionRequest" in str(DomainReference.execute.__annotations__["request"])
    assert DomainExecutionRequest is not None

    class Incomplete:
        def execute(self, request):
            return request

    assert not isinstance(Incomplete(), DomainReference)

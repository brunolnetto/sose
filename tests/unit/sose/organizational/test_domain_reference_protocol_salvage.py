"""Contract regression tests for historical typed DomainReference prototypes."""
from __future__ import annotations

from types import MappingProxyType
import pytest
from pydantic import ValidationError

from sose.organizational.domain_reference import (
    DomainReference, ExperimentObservation, ParameterDefinition, ParameterSpaceSpec,
)
from sose.organizational.experiment import ParameterRange
from sose.organizational.model_spec import EvidenceClass


def _param(name: str, default: float = 0.5):
    return ParameterDefinition(
        name=name, description="synthetic input", units="per_second",
        default=default, range=ParameterRange(low=0.1, high=1.0),
        evidence_class=EvidenceClass.ASSUMED,
    )


def test_parameter_space_identity_is_order_independent():
    first, second = _param("arrival"), _param("service", 0.8)
    a = ParameterSpaceSpec(parameters=(second, first))
    b = ParameterSpaceSpec(parameters=(first, second))
    assert a.canonical_payload() == b.canonical_payload()
    assert a.parameter_space_hash == b.parameter_space_hash
    assert tuple(p["name"] for p in a.canonical_payload()["parameters"]) == (
        "arrival", "service",
    )


def test_parameter_space_fails_closed_for_duplicate_and_invalid_inputs():
    with pytest.raises(ValidationError):
        ParameterSpaceSpec(parameters=())
    with pytest.raises(ValueError, match="duplicate parameter"):
        ParameterSpaceSpec(parameters=(_param("a"), _param("a")))
    with pytest.raises(ValueError, match="within"):
        _param("invalid", 10.0)


def test_observation_is_immutable_sorted_finite_and_roundtrippable():
    x = ExperimentObservation(
        world_hash="world:1", replication=1,
        metrics={"throughput": 3.0, "lead_time": 1.0},
    )
    assert isinstance(x.metrics, MappingProxyType)
    assert tuple(x.metrics) == ("lead_time", "throughput")
    assert x.model_dump()["metrics"] == {"lead_time": 1.0, "throughput": 3.0}
    with pytest.raises(TypeError):
        x.metrics["throughput"] = 9.0


@pytest.mark.parametrize("metric", [float("nan"), float("inf"), -float("inf")])
def test_observation_rejects_nonfinite_metrics(metric):
    with pytest.raises(ValidationError):
        ExperimentObservation(world_hash="w", replication=0, metrics={"v": metric})


def test_domain_reference_is_runtime_checkable_but_not_execution_certificate():
    class Incomplete:
        def descriptor(self):
            return None

    assert not isinstance(Incomplete(), DomainReference)
    assert not isinstance(object(), DomainReference)

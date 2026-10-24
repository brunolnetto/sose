"""#562 — bounded, typed DomainReference contract salvage from orphan branch.

These are structural interfaces; they do not certify an experiment or business domain.
"""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from sose.organizational.agency import AgencyLevel
from sose.organizational.domain_reference import (
    DomainReference, DomainReferenceIdentity, DomainReferenceDescriptor,
    ParameterDefinition, ParameterSpaceSpec, ExperimentObservation,
    AgencyCapabilitySpec, GroundTruthKind,
)
from sose.organizational.experiment import ParameterRange
from sose.organizational.model_spec import EvidenceClass


def _parameter(name: str) -> ParameterDefinition:
    return ParameterDefinition(
        name=name, description="Exogenous parameter", units="items/hour",
        default=1.0, range=ParameterRange(low=0.2, high=2.0),
        evidence_class=EvidenceClass.ASSUMED,
    )


def test_parameter_space_is_order_independent_and_rejects_duplicates():
    first = ParameterSpaceSpec(parameters=(_parameter("arrival"), _parameter("capacity")))
    permuted = ParameterSpaceSpec(parameters=(_parameter("capacity"), _parameter("arrival")))
    assert first.parameter_space_hash == permuted.parameter_space_hash
    assert first.canonical_payload() == permuted.canonical_payload()
    assert len(first.parameter_space_hash) == 64
    with pytest.raises(ValidationError, match="duplicate parameter names"):
        ParameterSpaceSpec(parameters=(_parameter("arrival"), _parameter("arrival")))


@pytest.mark.parametrize("metrics", [
    {"throughput": float("nan")},
    {"throughput": float("inf")},
    {"throughput": float("-inf")},
    {" ": 2.0},
    {"throughput": True},
])
def test_observation_rejects_non_finite_or_ambiguous_metrics(metrics):
    with pytest.raises((ValidationError, TypeError, ValueError)):
        ExperimentObservation(world_hash="world:1", replication=0, metrics=metrics)


def test_observation_freezes_projection_and_serializes_as_json():
    source = {"throughput": 12.0}
    observation = ExperimentObservation(world_hash="world:1", replication=1, metrics=source)
    source["throughput"] = 999.0
    assert observation.metrics["throughput"] == 12.0
    with pytest.raises(TypeError):
        observation.metrics["throughput"] = 15.0
    assert observation.model_dump(mode="json")["metrics"] == {"throughput": 12.0}
    with pytest.raises(ValidationError):
        ExperimentObservation(world_hash="", replication=0, metrics={"x": 1.0})


def test_protocol_is_structural_and_does_not_authorize_unsupported_policy():
    identity = DomainReferenceIdentity(
        domain="synthetic_queue_reference", reference_id="queue-ref",
        reference_version="1", process_manifest_domain="synthetic_queue_reference",
        specification_path="docs/organizational/evidence/synthetic-reference-v1/RESULT.md",
    )
    cap = AgencyCapabilitySpec(level=AgencyLevel.A0, capability_id="fixed-policy")
    descriptor = DomainReferenceDescriptor(
        identity=identity, parameters=(_parameter("arrival"),),
        agency_capabilities=(cap,), ground_truth_kinds=(GroundTruthKind.MECHANISTIC,),
    )

    class Reference:
        @property
        def identity(self):
            return identity
        def descriptor(self):
            return descriptor
        def parameter_space(self):
            return ParameterSpaceSpec(parameters=descriptor.parameters)
        def agency_capabilities(self):
            return (cap,)
        def ground_truth(self, world):
            return ()
        def classify_regime(self, world):
            return "stable"
        def execute(self, *, protocol, world, root_seed, replication):
            return None
        def observation(self, result):
            return ExperimentObservation(world_hash="world:1", replication=0,
                                         metrics={"throughput": 1.0})

    assert isinstance(Reference(), DomainReference)
    assert not isinstance(object(), DomainReference)
    assert set(c.level for c in Reference().agency_capabilities()) == {AgencyLevel.A0}

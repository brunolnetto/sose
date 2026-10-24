from sose.organizational.domain_reference import DomainReference, ExperimentObservation, ParameterSpaceSpec


def test_reference_contract_types_are_available():
    assert DomainReference is not None
    assert ExperimentObservation is not None
    assert ParameterSpaceSpec is not None

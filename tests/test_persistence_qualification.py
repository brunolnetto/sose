import pytest

from sose.persistence.qualification import (
    ConcurrencyEnvelope,
    PersistenceQualification,
    PersistenceTier,
    REQUIRED_AUTHORITATIVE_TESTS,
)


def test_authoritative_is_earned_by_qualification_not_inferred():
    qualification = PersistenceQualification(
        tier=PersistenceTier.AUTHORITATIVE,
        suite_version="1.0",
        passed_tests=REQUIRED_AUTHORITATIVE_TESTS,
        concurrency=ConcurrencyEnvelope(max_writers=1, distributed=False),
    )

    assert qualification.authoritative is True
    assert qualification.tier is PersistenceTier.AUTHORITATIVE


def test_non_authoritative_qualification_is_explicit():
    qualification = PersistenceQualification(
        tier=PersistenceTier.DURABLE,
        suite_version="1.0",
    )

    assert qualification.authoritative is False


@pytest.mark.parametrize("max_writers", [0, -1])
def test_concurrency_envelope_rejects_invalid_writer_counts(max_writers):
    with pytest.raises(ValueError, match="max_writers"):
        ConcurrencyEnvelope(max_writers=max_writers)


def test_authoritative_qualification_rejects_missing_evidence():
    with pytest.raises(ValueError, match="conformance evidence"):
        PersistenceQualification(
            tier=PersistenceTier.AUTHORITATIVE,
            suite_version="1.0",
        )

from sose.persistence.conformance_matrix import engine_conformance_matrix
from sose.persistence.qualification import (
    ConcurrencyEnvelope, PersistenceQualification, PersistenceTier,
    REQUIRED_AUTHORITATIVE_TESTS,
)
from sose.persistence.registry import builtin_persistence_registry


def test_cross_adapter_matrix_never_infers_authority_from_capability_flags():
    rows = {row.name: row for row in engine_conformance_matrix(builtin_persistence_registry())}
    assert rows["memory"].baseline_tier is None
    assert rows["jsonl"].baseline_tier is PersistenceTier.DURABLE
    assert rows["sqlite"].baseline_tier is PersistenceTier.DURABLE
    assert rows["sqlite_incremental"].baseline_tier is PersistenceTier.DURABLE
    assert rows["sqlite_incremental"].authoritative is False


def test_cross_adapter_matrix_promotes_only_explicit_qualification_evidence():
    qualification = PersistenceQualification(
        tier=PersistenceTier.AUTHORITATIVE,
        suite_version="1.0",
        passed_tests=REQUIRED_AUTHORITATIVE_TESTS,
        concurrency=ConcurrencyEnvelope(max_writers=1, distributed=False),
    )
    rows = {
        row.name: row
        for row in engine_conformance_matrix(
            builtin_persistence_registry(),
            qualifications={"sqlite_incremental": qualification},
        )
    }
    assert rows["sqlite_incremental"].baseline_tier is PersistenceTier.AUTHORITATIVE
    assert rows["sqlite_incremental"].authoritative is True
    assert rows["sqlite_incremental"].passed_tests == REQUIRED_AUTHORITATIVE_TESTS
    assert rows["sqlite"].baseline_tier is PersistenceTier.DURABLE

import pytest

from sose.persistence.authoritative_conformance import (
    AuthoritativePersistenceConformanceSuite,
    ConformanceResult,
    ConformanceCheckUnsupported,
)
from sose.persistence.qualification import (ConcurrencyEnvelope, REQUIRED_AUTHORITATIVE_TESTS, PersistenceTier)


class Harness:
    concurrency = ConcurrencyEnvelope(max_writers=1, distributed=False)

    def __init__(self, *, unsupported=(), failing=()):
        self.unsupported = set(unsupported)
        self.failing = set(failing)
        self.calls = []

    def _check(self, name):
        self.calls.append(name)
        if name in self.unsupported:
            raise ConformanceCheckUnsupported(name)
        if name in self.failing:
            raise AssertionError(name)

    def atomic_uow(self): self._check("atomic_uow")
    def read_after_commit(self): self._check("read_after_commit")
    def conditional_ownership(self): self._check("conditional_ownership")
    def stale_owner_fencing(self): self._check("stale_owner_fencing")
    def fresh_process_reconstruction(self): self._check("fresh_process_reconstruction")
    def deterministic_continuation(self): self._check("deterministic_continuation")
    def terminal_identity_monotonicity(self): self._check("terminal_identity_monotonicity")
    def schema_migration(self): self._check("schema_migration")


def test_suite_runs_every_required_check_and_issues_qualification():
    harness = Harness()
    suite = AuthoritativePersistenceConformanceSuite(harness)

    result = suite.run()
    qualification = suite.qualify(result)

    assert result.authoritative is True
    assert result.passed == REQUIRED_AUTHORITATIVE_TESTS
    assert set(harness.calls) == REQUIRED_AUTHORITATIVE_TESTS
    assert qualification.tier is PersistenceTier.AUTHORITATIVE
    assert qualification.passed_tests == REQUIRED_AUTHORITATIVE_TESTS


def test_suite_reports_failures_without_short_circuiting():
    harness = Harness(
        unsupported={"stale_owner_fencing"},
        failing={"atomic_uow"},
    )

    result = AuthoritativePersistenceConformanceSuite(harness).run()

    assert result.authoritative is False
    assert result.unsupported == frozenset({"stale_owner_fencing"})
    assert result.failed == (("atomic_uow", "AssertionError: atomic_uow"),)
    assert result.passed == REQUIRED_AUTHORITATIVE_TESTS - {
        "atomic_uow",
        "stale_owner_fencing",
    }


def test_suite_refuses_to_issue_authoritative_qualification_for_partial_evidence():
    suite = AuthoritativePersistenceConformanceSuite(
        Harness(unsupported={"conditional_ownership"})
    )

    with pytest.raises(RuntimeError, match="unsupported=conditional_ownership"):
        suite.qualify(suite.run())


def test_qualification_failure_message_includes_failed_checks():
    suite = AuthoritativePersistenceConformanceSuite(
        Harness(failing={"schema_migration"})
    )

    with pytest.raises(
        RuntimeError,
        match="failed=schema_migration",
    ):
        suite.qualify(suite.run())


def test_qualification_failure_without_missing_list_still_raises():
    harness = Harness()
    suite = AuthoritativePersistenceConformanceSuite(harness)
    result = ConformanceResult(
        passed=REQUIRED_AUTHORITATIVE_TESTS,
        failed=(("schema_migration", "AssertionError: schema_migration"),),
        unsupported=frozenset(),
        producer_id=id(harness),
        concurrency=harness.concurrency,
    )

    with pytest.raises(RuntimeError, match="failed=schema_migration") as excinfo:
        suite.qualify(result)
    assert "missing=" not in str(excinfo.value)


def test_qualification_uses_harness_concurrency_envelope():
    harness = Harness()
    result = AuthoritativePersistenceConformanceSuite(harness).run()
    qualification = AuthoritativePersistenceConformanceSuite(harness).qualify(result)

    assert qualification.concurrency == harness.concurrency


def test_qualification_rejects_evidence_from_another_harness():
    first = Harness()
    second = Harness()
    result = AuthoritativePersistenceConformanceSuite(first).run()

    with pytest.raises(RuntimeError, match="different harness or envelope"):
        AuthoritativePersistenceConformanceSuite(second).qualify(result)

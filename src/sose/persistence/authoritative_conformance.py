from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from .qualification import (
    REQUIRED_AUTHORITATIVE_TESTS,
    ConcurrencyEnvelope,
    PersistenceQualification,
    PersistenceTier,
)


SUITE_VERSION = "1.0"


class ConformanceCheckUnsupported(RuntimeError):
    """Raised when an adapter cannot yet demonstrate a required guarantee."""


class AuthoritativePersistenceHarness(Protocol):
    """Executable evidence provider for one adapter/configuration envelope."""

    def atomic_uow(self) -> None: ...
    def read_after_commit(self) -> None: ...
    def conditional_ownership(self) -> None: ...
    def stale_owner_fencing(self) -> None: ...
    def fresh_process_reconstruction(self) -> None: ...
    def deterministic_continuation(self) -> None: ...
    def terminal_identity_monotonicity(self) -> None: ...
    def schema_migration(self) -> None: ...


@dataclass(frozen=True, slots=True)
class ConformanceResult:
    passed: frozenset[str]
    failed: tuple[tuple[str, str], ...]
    unsupported: frozenset[str]

    @property
    def authoritative(self) -> bool:
        return (
            self.passed == REQUIRED_AUTHORITATIVE_TESTS
            and not self.failed
            and not self.unsupported
        )


@dataclass(frozen=True, slots=True)
class AuthoritativePersistenceConformanceSuite:
    """Run the authoritative promotion gate and issue evidence on success."""

    harness: AuthoritativePersistenceHarness
    concurrency: ConcurrencyEnvelope = ConcurrencyEnvelope()

    def run(self) -> ConformanceResult:
        passed: set[str] = set()
        failed: list[tuple[str, str]] = []
        unsupported: set[str] = set()

        for check_name in sorted(REQUIRED_AUTHORITATIVE_TESTS):
            check = getattr(self.harness, check_name)
            try:
                check()
            except ConformanceCheckUnsupported:
                unsupported.add(check_name)
            except Exception as exc:
                failed.append((check_name, f"{type(exc).__name__}: {exc}"))
            else:
                passed.add(check_name)

        return ConformanceResult(
            passed=frozenset(passed),
            failed=tuple(failed),
            unsupported=frozenset(unsupported),
        )

    def qualify(self) -> PersistenceQualification:
        result = self.run()
        if not result.authoritative:
            details = []
            if result.failed:
                details.append(
                    "failed=" + ", ".join(name for name, _ in result.failed)
                )
            if result.unsupported:
                details.append("unsupported=" + ", ".join(sorted(result.unsupported)))
            missing = REQUIRED_AUTHORITATIVE_TESTS - result.passed
            if missing:
                details.append("missing=" + ", ".join(sorted(missing)))
            raise RuntimeError(
                "authoritative persistence qualification failed"
                + (": " + "; ".join(details) if details else "")
            )

        return PersistenceQualification(
            tier=PersistenceTier.AUTHORITATIVE,
            suite_version=SUITE_VERSION,
            passed_tests=result.passed,
            concurrency=self.concurrency,
        )

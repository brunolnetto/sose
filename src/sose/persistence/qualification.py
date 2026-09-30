from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class PersistenceTier(StrEnum):
    ANALYTICAL = "analytical"
    DURABLE = "durable"
    AUTHORITATIVE = "authoritative"


@dataclass(frozen=True, slots=True)
class ConcurrencyEnvelope:
    """Operational limits under which a persistence qualification is valid."""

    max_writers: int | None = 1
    distributed: bool = False

    def __post_init__(self) -> None:
        if self.max_writers is not None and self.max_writers < 1:
            raise ValueError("max_writers must be positive or None")


@dataclass(frozen=True, slots=True)
class PersistenceQualification:
    """Executable evidence earned by an adapter, distinct from capability claims."""

    tier: PersistenceTier
    suite_version: str
    passed_tests: frozenset[str] = frozenset()
    concurrency: ConcurrencyEnvelope = ConcurrencyEnvelope()

    def __post_init__(self) -> None:
        if not self.suite_version:
            raise ValueError("suite_version must be non-empty")

    @property
    def authoritative(self) -> bool:
        return self.tier is PersistenceTier.AUTHORITATIVE

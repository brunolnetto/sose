"""Evidence-oriented Engine OLTP adapter matrix."""

from __future__ import annotations

from dataclasses import dataclass

from .qualification import PersistenceQualification, PersistenceTier
from .registry import PersistenceRegistry


@dataclass(frozen=True, slots=True)
class EngineAdapterConformance:
    name: str
    baseline_tier: PersistenceTier | None
    authoritative: bool
    suite_version: str | None = None
    passed_tests: frozenset[str] = frozenset()


def engine_conformance_matrix(
    registry: PersistenceRegistry,
    *,
    qualifications: dict[str, PersistenceQualification] | None = None,
) -> tuple[EngineAdapterConformance, ...]:
    """Describe claims separately from executable authoritative evidence."""
    evidence = qualifications or {}
    rows = []
    for adapter in registry.describe():
        qualification = evidence.get(adapter.name)
        if qualification is not None and qualification.authoritative:
            tier = PersistenceTier.AUTHORITATIVE
        elif adapter.capabilities.process_durable:
            tier = PersistenceTier.DURABLE
        else:
            tier = None
        rows.append(
            EngineAdapterConformance(
                name=adapter.name,
                baseline_tier=tier,
                authoritative=qualification is not None and qualification.authoritative,
                suite_version=None if qualification is None else qualification.suite_version,
                passed_tests=frozenset() if qualification is None else qualification.passed_tests,
            )
        )
    return tuple(rows)

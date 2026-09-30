from __future__ import annotations

from dataclasses import dataclass
from typing import Generic, Protocol, TypeVar


SemanticStateT = TypeVar("SemanticStateT")


class AuthoritativePersistenceHarness(Protocol[SemanticStateT]):
    """Backend-specific operations used by the technology-neutral suite."""

    def continuous_reference(self) -> SemanticStateT: ...
    def restarted_faulted_run(self) -> SemanticStateT: ...
    def semantic_projection(self, state: SemanticStateT) -> object: ...


@dataclass(frozen=True, slots=True)
class AuthoritativePersistenceConformanceSuite(Generic[SemanticStateT]):
    """Promotion gate above the base Persistence conformance contract.

    This initial skeleton deliberately does not certify any adapter. Concrete
    harnesses must supply restart/fault/concurrency/migration evidence.
    """

    harness: AuthoritativePersistenceHarness[SemanticStateT]

    def assert_restart_semantic_equivalence(self) -> None:
        reference = self.harness.continuous_reference()
        candidate = self.harness.restarted_faulted_run()
        candidate_projection = self.harness.semantic_projection(candidate)
        reference_projection = self.harness.semantic_projection(reference)
        if candidate_projection != reference_projection:
            raise RuntimeError(
                "authoritative persistence semantic equivalence failed"
            )

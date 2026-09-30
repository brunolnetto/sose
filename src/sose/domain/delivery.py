from __future__ import annotations

from dataclasses import dataclass

from .warehouse import DomainMutation


@dataclass(frozen=True, slots=True)
class DomainDelivery:
    """Engine-OLTP record proving a domain mutation still needs delivery."""

    mutation: DomainMutation
    attempts: int = 0
    last_error: str | None = None

    @property
    def mutation_id(self) -> str:
        return self.mutation.mutation_id

    def __post_init__(self) -> None:
        if self.attempts < 0:
            raise ValueError("domain delivery attempts must be >= 0")

"""Opt-in causal reference factory; v1 identifier strings stay opaque.

Callers pass kind and identity separately to BoundaryMessage.create. The type
discriminator is durably serialized with the message, never inferred from a
reserved prefix that could collide with legacy event/command identifiers.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

CausalKind = Literal["boundary", "event"]


@dataclass(frozen=True, slots=True)
class CausalReference:
    kind: CausalKind
    identity: str

    def __post_init__(self) -> None:
        if self.kind not in ("boundary", "event"):
            raise ValueError("unsupported causal reference kind")
        if not self.identity or self.identity.strip() != self.identity:
            raise ValueError("causal reference identity must be nonempty")

    @classmethod
    def boundary(cls, message_id: str) -> "CausalReference":
        return cls("boundary", message_id)

    @classmethod
    def event(cls, event_id: str) -> "CausalReference":
        return cls("event", event_id)

    def as_message_fields(self) -> dict[str, str]:
        """Use with BoundaryMessage.create(**ref.as_message_fields())."""
        return {"causation_id": self.identity, "causation_kind": self.kind}

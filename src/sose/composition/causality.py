"""Opt-in causal reference factory; v1 identifier strings stay opaque.

Callers pass kind and identity separately to BoundaryMessage.create. The type
discriminator is durably serialized with the message, never inferred from a
reserved prefix that could collide with legacy event/command identifiers.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
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


def matches_durable_message(prior, message) -> bool:
    """True when the durable ``prior`` already records ``message``.

    Typed causation (#644) changes only the ``causation_kind`` discriminator; the message
    identity does not include it. A message recorded untyped before the upgrade therefore
    still satisfies the same message re-derived with ``causation_kind="boundary"``; the
    durable record is kept as written, never rewritten.
    """
    if prior == message:
        return True
    return (
        prior.causation_kind is None
        and message.causation_kind == "boundary"
        and replace(prior, causation_kind="boundary") == message
    )

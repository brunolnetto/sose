"""Opt-in typed causal identities without rewriting frozen v1 boundary payloads.

The persisted causation_id remains a string. Only newly authored references
use the explicit 'boundary:<id>' or 'event:<id>' form. Untyped v1 causation IDs
keep their historical meaning and byte representation.
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
        if not self.identity or self.identity.strip() != self.identity or ":" in self.identity:
            raise ValueError("causal reference identity must be nonempty and unambiguous")

    @classmethod
    def boundary(cls, message_id: str) -> "CausalReference":
        return cls("boundary", message_id)

    @classmethod
    def event(cls, event_id: str) -> "CausalReference":
        return cls("event", event_id)

    def encode(self) -> str:
        return f"{self.kind}:{self.identity}"

    @classmethod
    def parse(cls, value: str | None) -> "CausalReference | None":
        if value is None:
            return None
        prefix, separator, identity = value.partition(":")
        if not separator or prefix not in ("boundary", "event"):
            return None
        return cls(prefix, identity)  # type: ignore[arg-type]

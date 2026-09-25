from __future__ import annotations

from uuid import UUID, uuid5

from .encoding import encode_deterministic_parts

SOSE_NAMESPACE = UUID("942319d8-a85f-5c79-a828-982f97dcbe56")


def deterministic_id(kind: str, *parts: object, namespace: UUID = SOSE_NAMESPACE) -> str:
    """Stable, collision-safe identity for replay/idempotency."""

    key = encode_deterministic_parts(kind, *parts).hex()
    return str(uuid5(namespace, key))

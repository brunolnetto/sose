from __future__ import annotations

from uuid import UUID, uuid5

SOSE_NAMESPACE = UUID("942319d8-a85f-5c79-a828-982f97dcbe56")


def deterministic_id(kind: str, *parts: object, namespace: UUID = SOSE_NAMESPACE) -> str:
    """Stable identity for replay/idempotency."""

    key = "|".join([kind, *(str(p) for p in parts)])
    return str(uuid5(namespace, key))

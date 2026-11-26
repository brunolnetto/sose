"""Pure claim checks for transition receipts (split from transition_authority).

No signing happens here: these functions compute receipt ids and digests and compare
signed claims with events and entities. ``transition_authority`` re-exports the
names other modules import from it.
"""
from __future__ import annotations

from hashlib import sha256
from json import dumps

from sose.core.attestation_chain import attribute_receipt_id
from sose.core.events import DomainEvent
from sose.core.identity import deterministic_id
from sose.domain.entity import Entity
from sose.persistence.codec import dumps as codec_dumps

ATTESTATION_EVENT = "engine.statechart_transition_attested"


def _encoded(data: dict) -> bytes:
    return dumps(data, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")


def _receipt_id(entity_type: str, entity_id: str, version: int) -> str:
    """Owner-independent receipt identity: one receipt per entity version.

    Entities are not owner-scoped, so a version claimed by any owner's receipt
    is claimed for every owner; this prevents a fork after a projection rewind.
    """
    return deterministic_id("engine-attested-transition", entity_type, entity_id, version)


def _attributes_sha256(entity: Entity) -> str:
    """Digest of the typed durable attribute encoding (fails on unsupported values)."""
    return sha256(codec_dumps(entity.attributes).encode("utf-8")).hexdigest()


def _initial_states(chart) -> set[str]:
    initial = {str(state.id) for state in chart.states if getattr(state, "initial", False)}
    if len(initial) != 1:
        raise ValueError("unauthenticated prestate: ambiguous chart initial state")
    return initial


def _predecessor_revision_claim(before: Entity) -> dict:
    """Set only after attested attribute saves, so older receipts keep their claims."""
    return {"predecessor_revision": before.attribute_revision} if before.attribute_revision else {}


def _require_unsigned_root(uow, before: Entity, initial) -> None:
    """A never-attested entity is at its chart's initial state with no receipt yet."""
    if before.version != 0 or before.attribute_revision != 0:
        return
    if (initial is not None and before.state not in initial) or uow.get_event(
        _receipt_id(before.entity_type, before.id, 1),
    ) is not None:
        raise ValueError(
            "unauthenticated prestate: version-zero entity must have "
            "its initial chart state and no transition history"
        )


def _envelope_mismatch(receipt: DomainEvent, event: DomainEvent) -> bool:
    pairs = (
        (receipt.name, ATTESTATION_EVENT), (receipt.causation_id, event.event_id),
        (receipt.owner_id, event.owner_id), (receipt.entity_type, event.entity_type),
        (receipt.entity_id, event.entity_id), (receipt.occurred_at, event.occurred_at),
        (receipt.tick, event.tick), (receipt.correlation_id, event.correlation_id),
    )
    return (
        type(event.tick) is not int or type(receipt.tick) is not int
        or any(left != right for left, right in pairs)
    )


def _evidence_mismatch(data: dict, event: DomainEvent) -> bool:
    expected = {
        "event_name": event.name,
        "event_payload_sha256": sha256(_encoded(dict(event.payload))).hexdigest(),
        "event_id": event.event_id, "owner_id": event.owner_id,
        "entity_id": event.entity_id, "entity_type": event.entity_type,
        "occurred_at": event.occurred_at.isoformat(), "tick": event.tick,
        "correlation_id": event.correlation_id,
        "trigger": event.payload.get("trigger"),
        "from_state": event.payload.get("from_state"),
        "to_state": event.payload.get("to_state"),
        "command_id": event.causation_id,
    }
    typed = (data.get("tick"), data.get("from_version"), data.get("to_version"))
    return (
        event.name != "entity.state_transition"
        or any(data.get(key) != value for key, value in expected.items())
        or any(type(value) is not int for value in typed)
        or data["to_version"] != data["from_version"] + 1
        or not isinstance(data.get("attributes_sha256"), str)
    )


def _require_anchored(claims: dict, before: Entity, initial: set[str], owner_id: str) -> None:
    """The head receipt must link to the previous version, or start at the chart root."""
    if claims["owner_id"] != owner_id:
        raise ValueError("trusted predecessor has foreign owner")
    version = before.version
    revision = claims.get("predecessor_revision", 0)
    if revision:
        expected = attribute_receipt_id(before.entity_type, before.id, version - 1, revision)
    else:
        expected = None if version == 1 else _receipt_id(
            before.entity_type, before.id, version - 1)
    if claims.get("predecessor_receipt_id") != expected:
        raise ValueError("trusted predecessor chain has missing version")
    if version == 1 and initial is not None and claims["from_state"] not in initial:
        raise ValueError("trusted predecessor chain does not start at the initial state")


def _require_projection(claims: dict, before: Entity, source: DomainEvent) -> None:
    """The stored entity must be exactly the signed terminal projection."""
    if (
        claims["to_version"] != before.version
        or claims["to_state"] != before.state
        or claims["attributes_sha256"] != _attributes_sha256(before)
    ):
        raise ValueError("authenticated StateChart receipt contradicts terminal projection")
    if before.updated_at != source.occurred_at:
        raise ValueError("trusted predecessor chain contradicts committed projection")

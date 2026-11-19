"""Incremental durable ledger guarding the OD-3 operational queue projection (#463).

Every queue is anchored by one deterministic ``od3.queue_anchored`` event
(written at seed time, or adopted on the first new-format application of a
queue seeded before the ledger existed). Every ``od3.queue_effect_applied``
written afterwards links to its predecessor effect (or the anchor) and records
running totals: cumulative paid policy time, queue version, last action time
and a digest of the full queue items before and after the effect.

Each effect also claims a deterministic per-version slot event
(``od3.queue_version_claimed``), so a projection re-saved at an older but
internally consistent ledger state is detected by the next slot being taken.

Admission verifies only the latest effect (named by the projection's
``ledger_head``), its paired decision, its predecessor and two version slots,
all fetched by event id: no log enumeration or deep copy of the log; the
per-id lookup cost is the adapter's (the memory-derived unit of work scans
its event list). Only events owned by this queue's owner count as evidence.

Event ids are global per store: an event of another owner occupying one of
this queue's deterministic ids (anchor, decision, effect, slot) makes the
lookup fail closed. That store-level limit is not solved here (#545).

This is a consistency guard against a forged *projection*, not certified
actor time: a public writer that can append arbitrary owner-scoped events is
still outside the trust boundary (#545).
"""
from __future__ import annotations

import hashlib
import json

from sose.core.events import DomainEvent
from sose.core.identity import deterministic_id
from sose.domain.entity import Entity
from sose.organizational.od3_decisions import _utc

ANCHOR_EVENT = "od3.queue_anchored"
EFFECT_EVENT = "od3.queue_effect_applied"
DECISION_EVENT = "od3.policy_decided"
SLOT_EVENT = "od3.queue_version_claimed"
_DECISION_FIELDS = (
    "policy", "matched_policy", "action", "selected_item_id", "before", "after", "cost_us",
)


def _fail(reason: str) -> None:
    raise ValueError(f"OD-3 charged queue {reason}")


def items_digest(records: list) -> str:
    """Bind every durable item attribute (and order) in one canonical digest."""
    canonical = json.dumps(records, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def anchor_id(queue_id: str) -> str:
    return deterministic_id("od3-queue-anchor", queue_id)


def slot_id(queue_id: str, version: int) -> str:
    """One durable claim per queue version: a later version proves a newer head."""
    return deterministic_id("od3-queue-slot", queue_id, version)


def anchor_event(queue: Entity, *, owner_id: str, origin: str) -> DomainEvent:
    """Record the projection a ledger starts from (``seed`` or legacy ``adopted``)."""
    attrs = queue.attributes
    at = _utc(queue.updated_at)
    return DomainEvent(
        event_id=anchor_id(queue.id), name=ANCHOR_EVENT,
        entity_type=queue.entity_type, entity_id=queue.id, owner_id=owner_id,
        occurred_at=at, causation_id=None, correlation_id=queue.id,
        payload={
            "origin": origin, "items_digest": items_digest(attrs["items"]),
            "paid_policy_us": attrs["paid_policy_us"],
            "queue_version": queue.version,
            "last_action_at": attrs["last_action_at"],
            "last_observed_at": attrs["last_observed_at"],
        },
    )


def _owned(uow, event_id, *, name: str, queue: Entity, owner_id: str):
    """Fetch by id; a foreign or differently named event is not our evidence."""
    event = uow.get_event(event_id) if isinstance(event_id, str) else None
    if event is None or event.owner_id != owner_id or event.name != name:
        return None
    if name != DECISION_EVENT and (
        event.entity_type != queue.entity_type or event.entity_id != queue.id
    ):
        return None
    return event


def _state(event: DomainEvent, *, anchor: bool) -> dict:
    """Running ledger totals recorded by an anchor or a linked effect."""
    payload = event.payload
    observed = payload.get("last_observed_at") if anchor else _utc(event.occurred_at).isoformat()
    return {
        "items_digest": payload.get("items_digest"),
        "paid_policy_us": payload.get("paid_policy_us"),
        "queue_version": payload.get("queue_version"),
        "last_action_at": payload.get("last_action_at"),
        "last_observed_at": observed,
        "at": _utc(event.occurred_at),
    }


def _check_decision(uow, head: DomainEvent, *, queue: Entity, owner_id: str) -> None:
    decision = _owned(uow, head.causation_id, name=DECISION_EVENT, queue=queue,
                      owner_id=owner_id)
    if (
        decision is None or decision.occurred_at != head.occurred_at
        or head.event_id != deterministic_id("od3-queue-effect", queue.id, decision.event_id)
        or head.correlation_id != decision.correlation_id
        or any(head.payload.get(key) != decision.payload.get(key) for key in _DECISION_FIELDS)
    ):
        _fail("causal decision ledger inconsistent")


def _check_effect_shape(head: DomainEvent) -> None:
    payload = head.payload
    cost = payload.get("cost_us")
    at = _utc(head.occurred_at).isoformat()
    contact = at if payload.get("action") == "CONTACT" else None
    if (
        type(cost) is not int or cost < 0
        or type(payload.get("queue_version")) is not int
        or type(payload.get("paid_policy_us")) is not int
        or not isinstance(payload.get("items_digest"), str)
        or payload.get("contact_at") != contact
    ):
        _fail("effect evidence malformed")


def _previous_state(uow, head: DomainEvent, anchor: DomainEvent, *, queue, owner_id) -> dict:
    previous_id = head.payload.get("previous_effect_id")
    if previous_id is None:
        return _state(anchor, anchor=True)
    previous = _owned(uow, previous_id, name=EFFECT_EVENT, queue=queue, owner_id=owner_id)
    if previous is None or "items_digest" not in previous.payload:
        _fail("effect chain broken")
    return _state(previous, anchor=False)


def _check_link(head: DomainEvent, previous: dict) -> None:
    payload = head.payload
    at = _utc(head.occurred_at)
    acted = at.isoformat() if payload["action"] != "NOOP" else previous["last_action_at"]
    if type(previous["queue_version"]) is not int or type(previous["paid_policy_us"]) is not int:
        _fail("ledger anchor or predecessor malformed")
    if (
        payload["queue_version"] != previous["queue_version"] + 1
        or payload["paid_policy_us"] != previous["paid_policy_us"] + payload["cost_us"]
        or payload.get("before_items_digest") != previous["items_digest"]
        or payload.get("last_action_at") != acted
        or at < previous["at"]
    ):
        _fail("cumulative cost or version differs from linked effects")


def _check_projection(queue: Entity, state: dict) -> None:
    attrs = queue.attributes
    if (
        attrs["paid_policy_us"] != state["paid_policy_us"]
        or queue.version != state["queue_version"]
        or len(attrs["applied_observations"]) != queue.version
        or attrs["last_action_at"] != state["last_action_at"]
        or attrs["last_observed_at"] != state["last_observed_at"]
        or queue.updated_at is None or _utc(queue.updated_at) != state["at"]
        or items_digest(attrs["items"]) != state["items_digest"]
    ):
        _fail("projection contradicts durable effects")


def _check_head_projection(queue: Entity, head: DomainEvent) -> None:
    payload = head.payload
    items = queue.attributes["items"]
    entry = queue.attributes["applied_observations"].get(payload.get("observation_id"))
    if (
        not isinstance(entry, dict) or entry.get("effect_event_id") != head.event_id
        or [item["item_id"] for item in items] != payload["after"]
    ):
        _fail("projection contradicts durable effects")
    if payload["action"] == "CONTACT" and any(
        item["item_id"] == payload["selected_item_id"]
        and item["last_contact_at"] != payload["contact_at"]
        for item in items
    ):
        _fail("projection contradicts durable contact")


def _check_latest(uow, queue: Entity, *, owner_id: str) -> None:
    """Refuse a projection rolled back to an older (still consistent) ledger state."""
    head_id = queue.attributes["ledger_head"]
    if head_id is not None:
        slot = _owned(uow, slot_id(queue.id, queue.version), name=SLOT_EVENT,
                      queue=queue, owner_id=owner_id)
        if slot is None or slot.payload.get("effect_event_id") != head_id:
            _fail("ledger head does not hold its version slot")
    if uow.get_event(slot_id(queue.id, queue.version + 1)) is not None:
        _fail("projection rolled back behind a later durable effect")


def verify_queue_ledger(uow, queue: Entity, *, owner_id: str) -> DomainEvent | None:
    """Check the projection against the latest ledger entry; ``None`` means legacy.

    A queue without an anchor was seeded before the ledger existed and has no
    durable digest to compare with; it is adopted on its next application.
    """
    anchor = _owned(uow, anchor_id(queue.id), name=ANCHOR_EVENT, queue=queue,
                    owner_id=owner_id)
    attrs = queue.attributes
    if anchor is None:
        if "ledger_head" in attrs or uow.get_event(anchor_id(queue.id)) is not None:
            _fail("ledger anchor missing or foreign")
        return None
    if "ledger_head" not in attrs:
        _fail("projection dropped its ledger head")
    _check_latest(uow, queue, owner_id=owner_id)
    head_id = attrs["ledger_head"]
    if head_id is None:
        _check_projection(queue, _state(anchor, anchor=True))
        return anchor
    head = _owned(uow, head_id, name=EFFECT_EVENT, queue=queue, owner_id=owner_id)
    if head is None:
        _fail("ledger head lacks durable effect")
    _check_effect_shape(head)
    _check_decision(uow, head, queue=queue, owner_id=owner_id)
    _check_link(head, _previous_state(uow, head, anchor, queue=queue, owner_id=owner_id))
    _check_projection(queue, _state(head, anchor=False))
    _check_head_projection(queue, head)
    return anchor


def append_anchor(uow, queue: Entity, *, owner_id: str, origin: str) -> None:
    """Durably anchor the current projection; refuse a pre-occupied identity."""
    if uow.get_event(anchor_id(queue.id)) is not None:
        raise ValueError("OD-3 queue ledger anchor identity already exists")
    uow.append_event(anchor_event(queue, owner_id=owner_id, origin=origin))
    queue.attributes["ledger_head"] = None


def ensure_queue_ledger(uow, queue: Entity, *, owner_id: str) -> None:
    """Verify an anchored queue, or adopt a legacy (pre-ledger) queue as-is.

    Adoption trusts the legacy projection once: verification starts from the
    adoption anchor, since no durable digest of earlier history exists.
    """
    if verify_queue_ledger(uow, queue, owner_id=owner_id) is not None:
        return
    if len(queue.attributes["applied_observations"]) != queue.version:
        _fail("legacy projection inconsistent with its version")
    append_anchor(uow, queue, owner_id=owner_id, origin="adopted")


def ledger_fields(queue: Entity, before_items: list) -> dict:
    """Effect fields linking the updated projection to the verified ledger head."""
    return {
        "previous_effect_id": queue.attributes["ledger_head"],
        "before_items_digest": items_digest(before_items),
        "items_digest": items_digest(queue.attributes["items"]),
        "last_action_at": queue.attributes["last_action_at"],
    }


def claim_version_slot(uow, queue: Entity, effect: DomainEvent) -> None:
    """Append the per-version claim for a new effect; refuse an occupied slot."""
    version = effect.payload["queue_version"]
    event_id = slot_id(queue.id, version)
    if uow.get_event(event_id) is not None:
        raise ValueError("OD-3 queue version slot already claimed")
    uow.append_event(DomainEvent(
        event_id=event_id, name=SLOT_EVENT,
        entity_type=queue.entity_type, entity_id=queue.id, owner_id=effect.owner_id,
        occurred_at=effect.occurred_at, causation_id=effect.event_id,
        correlation_id=effect.correlation_id,
        payload={"queue_version": version, "effect_event_id": effect.event_id},
    ))

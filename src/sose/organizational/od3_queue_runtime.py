"""Bounded OD-3 queue runtime: atomic queue effects and charged policy decisions.

This is a durable single-queue operational reference, not a physical scheduler,
an authenticated Engine transition or an OD-5b actor-time scientific certificate.
The queue is authoritative *within this service's UoW*; callers with public UoW
write access are outside its trust boundary (#545).
"""
from __future__ import annotations

from contextlib import nullcontext
from datetime import datetime
from dataclasses import replace

from sose.core.events import DomainEvent
from sose.core.identity import deterministic_id
from sose.domain.entity import Entity
from sose.organizational.od3_decisions import (
    OD3Policy, QueuedWork, _utc, decide_od3, od3_decision_event,
)

QUEUE_KIND = "od3_operational_queue"
_SCHEMA_VERSION = 1
_ITEM_FIELDS = frozenset({
    "item_id", "owner_id", "arrived_at", "service_us", "last_contact_at", "blocked",
})


def _identity(value: str, label: str) -> None:
    if not isinstance(value, str) or not value.strip() or value != value.strip():
        raise ValueError(f"OD-3 {label} requires canonical nonblank identity")


def _lock(store, queue_id: str):
    factory = getattr(store, "business_resource_guard", None)
    return factory("od3-queue:" + queue_id) if callable(factory) else nullcontext()


def _record(item: QueuedWork) -> dict:
    return {
        "item_id": item.item_id,
        "owner_id": item.owner_id,
        "arrived_at": _utc(item.arrived_at).isoformat(),
        "service_us": item.service_us,
        "last_contact_at": (
            _utc(item.last_contact_at).isoformat()
            if item.last_contact_at is not None else None
        ),
        "blocked": item.blocked,
    }


def _restore(raw: dict) -> QueuedWork:
    if not isinstance(raw, dict) or set(raw) != _ITEM_FIELDS:
        raise ValueError("OD-3 durable queue item schema mismatch")
    if type(raw["blocked"]) is not bool:
        raise ValueError("OD-3 durable queue has non-boolean blocked flag")
    try:
        return QueuedWork(
            item_id=raw["item_id"], owner_id=raw["owner_id"],
            arrived_at=datetime.fromisoformat(raw["arrived_at"]),
            service_us=raw["service_us"],
            last_contact_at=(
                datetime.fromisoformat(raw["last_contact_at"])
                if raw["last_contact_at"] is not None else None
            ),
            blocked=raw["blocked"],
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("OD-3 durable queue item is invalid") from exc


def _read(queue: Entity, *, owner_id: str, queue_key: str) -> tuple[QueuedWork, ...]:
    attrs = queue.attributes
    if (
        queue.entity_type != QUEUE_KIND or queue.state != "ready"
        or not isinstance(attrs, dict) or set(attrs) != {
            "schema_version", "owner_id", "queue_key", "items",
            "paid_policy_us", "last_action_at", "last_observed_at",
            "applied_observations",
        }
        or attrs["schema_version"] != _SCHEMA_VERSION
        or attrs["owner_id"] != owner_id or attrs["queue_key"] != queue_key
        or queue.id != deterministic_id("od3-queue", owner_id, queue_key)
        or type(attrs["paid_policy_us"]) is not int or attrs["paid_policy_us"] < 0
        or not isinstance(attrs["items"], list)
        or not isinstance(attrs["applied_observations"], dict)
    ):
        raise ValueError("OD-3 durable queue provenance/shape mismatch")
    for field in ("last_action_at", "last_observed_at"):
        if attrs[field] is not None:
            if not isinstance(attrs[field], str):
                raise ValueError("OD-3 durable queue timestamp malformed")
            _utc(datetime.fromisoformat(attrs[field]))
    items = tuple(_restore(item) for item in attrs["items"])
    if (
        any(item.owner_id != owner_id for item in items)
        or len({item.item_id for item in items}) != len(items)
    ):
        raise ValueError("OD-3 durable queue contains duplicate or foreign items")
    return items



def _verify_effect_cost_ledger(uow, queue: Entity, *, owner_id: str) -> None:
    """Reconcile charged policy time with canonical durable queue decisions.

    This is consistency, not certified actor-time or physical service capacity.
    A public writer can still forge untrusted sources without #545 authority.
    """
    # Materialize one transaction-consistent snapshot and index event IDs
    # once. Repeated uow.get_event() scans would make a long-lived queue's
    # next policy observation O(N * E) within the business guard.
    all_events = tuple(uow.events())
    by_id = {}
    for event in all_events:
        by_id.setdefault(event.event_id, []).append(event)
    effects = tuple(event for event in all_events if (
        event.name == "od3.queue_effect_applied"
        and event.entity_type == QUEUE_KIND and event.entity_id == queue.id
    ))
    applied = queue.attributes["applied_observations"]
    if len(effects) != queue.version or len(applied) != len(effects):
        raise ValueError("OD-3 charged queue history incomplete or duplicated")
    paid = 0
    previous_at = _utc(queue.created_at)
    previous_order = None
    last_action_at = None
    seen = set()
    for ordinal, effect in enumerate(effects, start=1):
        evidence = effect.payload
        observation_id = evidence.get("observation_id")
        matches = by_id.get(effect.causation_id, ())
        decision = matches[0] if len(matches) == 1 else None
        at = _utc(effect.occurred_at)
        cost = evidence.get("cost_us")
        if (
            not isinstance(observation_id, str) or not observation_id.strip()
            or observation_id in seen
            or effect.owner_id != owner_id or at < previous_at
            or decision is None or decision.name != "od3.policy_decided"
            or decision.owner_id != owner_id or decision.occurred_at != at
            or effect.event_id != deterministic_id(
                "od3-queue-effect", queue.id, decision.event_id,
            )
            or effect.correlation_id != decision.correlation_id
            or type(cost) is not int or cost < 0
            or type(evidence.get("queue_version")) is not int
            or evidence["queue_version"] != ordinal
            or (
                previous_order is not None
                and evidence.get("before") != previous_order
            )
            or not isinstance(evidence.get("after"), list)
            or evidence.get("policy") != decision.payload.get("policy")
            or evidence.get("matched_policy") != decision.payload.get("matched_policy")
            or evidence.get("action") != decision.payload.get("action")
            or evidence.get("selected_item_id") != decision.payload.get("selected_item_id")
            or evidence.get("before") != decision.payload.get("before")
            or evidence.get("after") != decision.payload.get("after")
            or cost != decision.payload.get("cost_us")
            or applied.get(observation_id) != {
                "request_id": applied.get(observation_id, {}).get("request_id")
                if isinstance(applied.get(observation_id), dict) else None,
                "effect_event_id": effect.event_id,
            }
        ):
            raise ValueError("OD-3 charged queue causal decision ledger inconsistent")
        paid += cost
        if evidence.get("paid_policy_us") != paid:
            raise ValueError("OD-3 charged queue cumulative cost differs from events")
        if evidence["action"] != "NOOP":
            last_action_at = at.isoformat()
        previous_order = evidence["after"]
        previous_at = at
        seen.add(observation_id)
    if (
        type(queue.attributes.get("paid_policy_us")) is not int
        or queue.attributes["paid_policy_us"] != paid
        or queue.attributes.get("last_observed_at") != (
            previous_at.isoformat() if effects else None
        )
        or queue.attributes.get("last_action_at") != last_action_at
        or queue.updated_at != previous_at
        or (previous_order is not None and previous_order != [
            item["item_id"] for item in queue.attributes["items"]
        ])
    ):
        raise ValueError("OD-3 charged queue projection contradicts durable effects")


def seed_od3_queue(
    store, *, owner_id: str, queue_key: str,
    items: tuple[QueuedWork, ...], at: datetime,
) -> str:
    """Admit immutable initial work order; identity cannot be overwritten."""
    _identity(owner_id, "owner")
    _identity(queue_key, "queue key")
    now = _utc(at)
    if not isinstance(items, tuple) or any(not isinstance(item, QueuedWork) for item in items):
        raise TypeError("OD-3 initial queue requires tuple[QueuedWork, ...]")
    if (
        len({item.item_id for item in items}) != len(items)
        or any(
            item.owner_id != owner_id
            or not isinstance(item.blocked, bool)
            or type(item.blocked) is not bool
            or _utc(item.arrived_at) > now
            or (item.last_contact_at is not None and _utc(item.last_contact_at) > now)
            for item in items
        )
    ):
        raise ValueError("OD-3 queue admission rejects duplicate, foreign or future work")
    queue_id = deterministic_id("od3-queue", owner_id, queue_key)
    if getattr(store, "transaction_depth", 0):
        raise RuntimeError("OD-3 queue admission refuses a nested transaction")
    with _lock(store, queue_id):
        with store.transaction(owner_id=owner_id) as uow:
            if uow.get_entity(QUEUE_KIND, queue_id) is not None:
                raise ValueError("OD-3 queue has already been admitted")
            uow.save_entity(Entity(
                id=queue_id, entity_type=QUEUE_KIND, state="ready",
                created_at=now, updated_at=now,
                attributes={
                    "schema_version": _SCHEMA_VERSION, "owner_id": owner_id,
                    "queue_key": queue_key, "items": [_record(item) for item in items],
                    "paid_policy_us": 0, "last_action_at": None,
                    "last_observed_at": None, "applied_observations": {},
                },
            ))
    return queue_id


def apply_od3_queue_policy(
    store, *, owner_id: str, queue_key: str, observation_id: str,
    policy: OD3Policy, observed_at: datetime,
    matched_active_policy: OD3Policy | None = None,
) -> DomainEvent:
    """Atomically recompute, apply and charge one decision from committed queue.

    Never accept an externally computed decision as authorization. Replays of
    the same observation return its committed effect; incompatible replays fail.
    An actual service or actor capacity grant is deliberately NOT created.
    """
    for value, label in (
        (owner_id, "owner"), (queue_key, "queue key"),
        (observation_id, "observation"),
    ):
        _identity(value, label)
    if not isinstance(policy, OD3Policy):
        raise TypeError("OD-3 queue requires typed policy")
    now = _utc(observed_at)
    queue_id = deterministic_id("od3-queue", owner_id, queue_key)
    request_id = deterministic_id(
        "od3-application-request", owner_id, queue_key, observation_id,
        now.isoformat(), policy.name, policy.matched_policy,
        policy.delay_threshold_us, policy.cooldown_us,
        policy.decision_cost_us, policy.starvation_bound_us,
        (
            matched_active_policy.name,
            matched_active_policy.delay_threshold_us,
            matched_active_policy.cooldown_us,
            matched_active_policy.decision_cost_us,
            matched_active_policy.starvation_bound_us,
        ) if matched_active_policy is not None else None,
    )
    # Memory snapshots are not nested transactions: an outer transaction
    # would otherwise silently overwrite an inner committed effect.
    if getattr(store, "transaction_depth", 0):
        raise RuntimeError("OD-3 queue mutation refuses a nested transaction")
    with _lock(store, queue_id):
        with store.transaction(owner_id=owner_id) as uow:
            queue = uow.get_entity(QUEUE_KIND, queue_id)
            if queue is None:
                raise KeyError(f"OD-3 operational queue not admitted: {queue_id}")
            items = _read(queue, owner_id=owner_id, queue_key=queue_key)
            applied = queue.attributes["applied_observations"]
            existing = applied.get(observation_id)
            if existing is not None:
                if (
                    not isinstance(existing, dict)
                    or set(existing) != {"request_id", "effect_event_id"}
                    or existing["request_id"] != request_id
                ):
                    raise ValueError("OD-3 observation replay conflicts with original request")
                event = uow.get_event(existing["effect_event_id"])
                if (
                    event is None or event.name != "od3.queue_effect_applied"
                    or event.entity_id != queue_id or event.owner_id != owner_id
                    or event.payload.get("observation_id") != observation_id
                ):
                    raise ValueError("OD-3 queue observation lacks durable effect evidence")
                return event

            if queue.created_at is None or now < _utc(queue.created_at):
                raise ValueError("OD-3 observation precedes queue admission")
            _verify_effect_cost_ledger(uow, queue, owner_id=owner_id)
            previous_observed = queue.attributes["last_observed_at"]
            if previous_observed is not None and now < _utc(datetime.fromisoformat(previous_observed)):
                raise ValueError("OD-3 queue observation time regressed")
            last_action = queue.attributes["last_action_at"]
            previous_decision_at = (
                _utc(datetime.fromisoformat(last_action)) if last_action is not None else None
            )
            decision = decide_od3(
                owner_id=owner_id, queue=items, policy=policy,
                observed_at=now, previous_decision_at=previous_decision_at,
                # Distinct queues may share external observation IDs and even
                # identical snapshots; operational diagnostic IDs must not.
                observation_id=deterministic_id(
                    "od3-operational-observation", queue_id, observation_id,
                ), matched_active_policy=matched_active_policy,
            )
            decided_event = od3_decision_event(decision)
            if uow.get_event(decided_event.event_id) is not None:
                raise ValueError("OD-3 queue decision identity already used")
            if decision.action == "CONTACT":
                changed = tuple(
                    replace(item, last_contact_at=now)
                    if item.item_id == decision.selected_item_id else item
                    for item in items
                )
            elif decision.action == "PRIORITIZE":
                by_id = {item.item_id: item for item in items}
                changed = tuple(by_id[item_id] for item_id in decision.after)
            else:
                changed = items
            if tuple(item.item_id for item in changed) != decision.after:
                raise ValueError("OD-3 applied queue differs from deterministic decision")
            before_paid = queue.attributes["paid_policy_us"]
            queue.attributes["items"] = [_record(item) for item in changed]
            queue.attributes["paid_policy_us"] = before_paid + decision.cost_us
            queue.attributes["last_observed_at"] = now.isoformat()
            if decision.action != "NOOP":
                queue.attributes["last_action_at"] = now.isoformat()
            effect_id = deterministic_id("od3-queue-effect", queue_id, decided_event.event_id)
            effect = DomainEvent(
                event_id=effect_id, name="od3.queue_effect_applied",
                entity_type=QUEUE_KIND, entity_id=queue_id, owner_id=owner_id,
                occurred_at=now, causation_id=decided_event.event_id,
                correlation_id=decided_event.correlation_id,
                payload={
                    "observation_id": observation_id,
                    "policy": decision.policy, "matched_policy": decision.matched_policy,
                    "action": decision.action, "selected_item_id": decision.selected_item_id,
                    "before": list(decision.before), "after": list(decision.after),
                    "cost_us": decision.cost_us, "paid_policy_us": before_paid + decision.cost_us,
                    "queue_version": queue.version + 1,
                    "contact_at": now.isoformat() if decision.action == "CONTACT" else None,
                },
            )
            if uow.get_event(effect_id) is not None:
                raise ValueError("OD-3 operational effect identity already exists")
            applied[observation_id] = {
                "request_id": request_id, "effect_event_id": effect_id,
            }
            queue.touch(now)
            uow.save_entity(queue)
            uow.append_event(decided_event)
            uow.append_event(effect)
            return effect

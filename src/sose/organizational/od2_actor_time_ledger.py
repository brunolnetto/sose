"""OD-2 opt-in v1 actor-time ledger: incremental fail-closed projection guard.

An actor enrolled with ``seed_actor(tracked_time=True)`` gets an immutable
``od2.actor_time_origin_recorded`` event. Every later native transition
appends one ``od2.actor_time_ledger_step`` event whose deterministic ID is
derived from (actor, sequence number). Each step carries the running totals
(per-state elapsed microseconds, paid cost, last effective time, state,
assigned work and a digest of the cause index) *after* that transition and
names its predecessor step, so the steps form an ID-addressed chain.

Before a new transition (or a same-cause replay) the mutable actor projection
must equal the totals of the chain head, the head must bind to its original
StateChart event and Command, and no successor step may already exist. Each
step is written only after its predecessor was verified, so verification
reads a constant number of records by ID (``get_event``/``get_command``) and
never scans the shared event log: cost is independent of total store size.

This is a consistency/fail-closed control on public UoW records, NOT an
independent source authorization or physical-capacity grant (see #545).
Markerless legacy actors are left untouched and remain unattested.
"""
from __future__ import annotations

from datetime import timezone
from hashlib import sha256
from json import dumps

from sose.core.events import Command, DomainEvent
from sose.core.identity import deterministic_id

ORIGIN_EVENT = "od2.actor_time_origin_recorded"
STEP_EVENT = "od2.actor_time_ledger_step"
STEP_ENTITY = "organizational_actor_time_ledger"
LEDGER_VERSION = 1
_LEDGER_ERRORS = (AttributeError, KeyError, TypeError, ValueError)


def origin_event_id(actor_id: str) -> str:
    return deterministic_id("od2-actor-time-origin-v1", actor_id)


def step_event_id(actor_id: str, seq: int) -> str:
    return deterministic_id("od2-actor-time-step-v1", actor_id, seq)


def tracked_attributes(tracked_time: bool) -> dict:
    """Projection marker for a prospectively tracked actor (empty if not)."""
    if type(tracked_time) is not bool:
        raise TypeError("tracked_time must be an explicit boolean")
    return {"tracked_time_version": LEDGER_VERSION} if tracked_time else {}


def _origin_payload(meta: dict) -> dict:
    return {
        "owner_id": meta.get("owner_id"),
        "model_spec_hash": meta.get("model_spec_hash"),
        "config_epoch": meta.get("config_epoch"),
        "replaces_actor_id": meta.get("replaces_actor_id"),
        "tracked_time_version": LEDGER_VERSION,
    }


def record_origin(uow, actor) -> None:
    """Append the immutable v1 origin event for a newly tracked actor."""
    meta = actor.attributes
    if meta.get("tracked_time_version") != LEDGER_VERSION:
        return
    if uow.get_event(origin_event_id(actor.id)) is not None:
        raise ValueError("OD-2 actor-time origin ID already committed; cannot enroll")
    uow.append_event(DomainEvent(
        event_id=origin_event_id(actor.id), name=ORIGIN_EVENT,
        entity_type=actor.entity_type, entity_id=actor.id,
        owner_id=meta["owner_id"], occurred_at=actor.created_at,
        correlation_id=deterministic_id("od2-actor-flow", meta["owner_id"], actor.id),
        payload=_origin_payload(meta),
    ))


def _nonzero_buckets(time_us) -> dict:
    """Canonical per-state buckets: only nonzero entries are significant.

    Comparing only present keys (missing == 0) keeps stored actors valid
    when a new lifecycle state is introduced later.
    """
    if not isinstance(time_us, dict):
        raise ValueError("OD-2 actor-time buckets malformed")
    for state, value in time_us.items():
        if not isinstance(state, str) or type(value) is not int or value < 0:
            raise ValueError("OD-2 actor-time bucket malformed")
    return {state: time_us[state] for state in sorted(time_us) if time_us[state]}


def _causes_digest(causes) -> str:
    if not isinstance(causes, dict):
        raise ValueError("OD-2 actor-time ledger lacks durable cause index")
    return sha256(dumps(
        causes, sort_keys=True, separators=(",", ":"), allow_nan=False,
    ).encode("utf-8")).hexdigest()


def _snapshot(actor) -> dict:
    """Running totals the projection claims after its latest transition."""
    meta = actor.attributes
    paid = meta["paid_transition_us"]
    if type(paid) is not int:
        raise ValueError("OD-2 actor-time paid cost malformed")
    return {
        "state": actor.state,
        "time_us": _nonzero_buckets(meta["time_us"]),
        "paid_transition_us": paid,
        "last_at": meta["last_at"],
        "assigned_work_id": meta["assigned_work_id"],
        "causes_digest": _causes_digest(meta["applied_causes"]),
    }


def _initial_totals(origin: DomainEvent) -> dict:
    return {
        "state": "admitted", "time_us": {}, "paid_transition_us": 0,
        "last_at": origin.occurred_at.astimezone(timezone.utc).isoformat(),
        "assigned_work_id": None, "causes_digest": _causes_digest({}),
    }


def _check_origin(actor, origin: DomainEvent | None) -> None:
    meta = actor.attributes
    if origin is None:
        raise ValueError("OD-2 actor-time enrollment source missing")
    marker = meta.get("tracked_time_version")
    actual = (
        origin.name, origin.entity_type, origin.entity_id, origin.owner_id,
        dict(origin.payload), origin.occurred_at, type(marker), marker,
    )
    expected = (
        ORIGIN_EVENT, actor.entity_type, actor.id, meta.get("owner_id"),
        _origin_payload(meta), actor.created_at, int, LEDGER_VERSION,
    )
    if actual != expected:
        raise ValueError("OD-2 actor-time enrollment source contradicts projection")


def _is_own(event: DomainEvent, name: str, entity_type: str, actor) -> bool:
    """True if ``event`` is a ledger record of ``name`` for this actor/owner."""
    return (
        event.name, event.entity_type, event.entity_id, event.owner_id,
    ) == (name, entity_type, actor.id, actor.attributes.get("owner_id"))


def _prev_event_id(actor_id: str, seq: int) -> str:
    return origin_event_id(actor_id) if seq == 1 else step_event_id(actor_id, seq - 1)


def _check_step_header(actor, head: DomainEvent | None, seq: int) -> dict:
    if head is None:
        raise ValueError("OD-2 actor-time ledger head missing for causal history")
    payload = dict(head.payload)
    actual = (
        head.name, head.entity_type, head.entity_id, head.owner_id,
        payload.get("version"), payload.get("seq"), payload.get("prev_event_id"),
    )
    expected = (
        STEP_EVENT, STEP_ENTITY, actor.id, actor.attributes.get("owner_id"),
        LEDGER_VERSION, seq, _prev_event_id(actor.id, seq),
    )
    if actual != expected or not isinstance(payload.get("after"), dict):
        raise ValueError("OD-2 actor-time ledger head contradicts actor lineage")
    return payload


def _check_step_source(uow, actor, head: DomainEvent, payload: dict) -> None:
    """Bind the head step to its original StateChart event and Command."""
    after = payload["after"]
    source = uow.get_event(payload["source_event_id"])
    command = uow.get_command(payload["command_id"])
    if source is None or command is None or not isinstance(command.payload, dict):
        raise ValueError("OD-2 actor-time missing original causal Command/event")
    actual = (
        source.name, source.entity_type, source.entity_id, source.owner_id,
        source.causation_id, source.occurred_at.astimezone(timezone.utc).isoformat(),
        source.payload.get("from_state"), source.payload.get("to_state"),
        head.causation_id, command.name, command.owner_id, command.entity_id,
        command.payload.get("external_cause_id"), command.payload.get("work_id"),
        command.payload.get("cost_us"),
        actor.attributes["applied_causes"].get(payload["cause_id"]),
    )
    expected = (
        "entity.state_transition", actor.entity_type, actor.id, head.owner_id,
        payload["command_id"], after["last_at"],
        payload["from_state"], after["state"],
        source.event_id, payload["action"], head.owner_id, actor.id,
        payload["cause_id"], payload["work_id"], payload["cost_us"],
        [payload["action"], after["last_at"], payload["work_id"], payload["cost_us"]],
    )
    if actual != expected:
        raise ValueError("OD-2 actor-time causal lineage contradicts original history")


def _expected_totals(uow, actor, origin: DomainEvent) -> dict:
    seq = len(actor.attributes["applied_causes"])
    successor = uow.get_event(step_event_id(actor.id, seq + 1))
    if successor is not None:
        if not _is_own(successor, STEP_EVENT, STEP_ENTITY, actor):
            raise ValueError("OD-2 actor-time ledger ID occupied by a foreign event")
        raise ValueError("OD-2 actor-time causal history truncated in projection")
    if seq == 0:
        return _initial_totals(origin)
    head = uow.get_event(step_event_id(actor.id, seq))
    payload = _check_step_header(actor, head, seq)
    _check_step_source(uow, actor, head, payload)
    return payload["after"]


def verify_time_ledger(uow, actor) -> None:
    """Fail closed unless a tracked actor projection equals its ledger head.

    Untracked actors (no projection marker and no genuine origin event for
    this actor) pass unchanged; a foreign event squatting on the predictable
    origin ID cannot affect them. A genuine origin always enforces, so
    stripping the marker is caught. Constant number of keyed reads; no
    event-log scan.
    """
    meta = actor.attributes
    if not isinstance(meta, dict):
        raise ValueError("OD-2 actor-time projection malformed")
    origin = uow.get_event(origin_event_id(actor.id))
    genuine = origin is not None and _is_own(origin, ORIGIN_EVENT, actor.entity_type, actor)
    if not genuine and meta.get("tracked_time_version") is None:
        return
    try:
        _check_origin(actor, origin)
        expected = _expected_totals(uow, actor, origin)
        actual = _snapshot(actor)
    except _LEDGER_ERRORS as exc:
        if str(exc).startswith("OD-2 actor-time"):
            raise
        raise ValueError("OD-2 actor-time projection malformed") from exc
    if actual != expected:
        raise ValueError("OD-2 actor-time projection contradicts committed time/cost ledger")


def record_step(uow, actor, command: Command, source: DomainEvent, cause_id: str) -> None:
    """Append the next ledger step for a tracked actor (no-op if untracked)."""
    meta = actor.attributes
    if meta.get("tracked_time_version") != LEDGER_VERSION:
        return
    seq = len(meta["applied_causes"])
    event_id = step_event_id(actor.id, seq)
    if uow.get_event(event_id) is not None:
        raise ValueError("OD-2 actor-time ledger step ID already committed")
    uow.append_event(DomainEvent(
        event_id=event_id, name=STEP_EVENT,
        entity_type=STEP_ENTITY, entity_id=actor.id,
        owner_id=command.owner_id, occurred_at=source.occurred_at,
        causation_id=source.event_id, correlation_id=command.correlation_id,
        payload={
            "version": LEDGER_VERSION, "seq": seq,
            "prev_event_id": _prev_event_id(actor.id, seq),
            "cause_id": cause_id, "command_id": command.command_id,
            "source_event_id": source.event_id, "action": command.name,
            "from_state": source.payload["from_state"],
            "work_id": command.payload["work_id"],
            "cost_us": command.payload["cost_us"],
            "after": _snapshot(actor),
        },
    ))

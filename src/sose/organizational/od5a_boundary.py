"""OD-5a durable physical-event ingress to existing fenced boundary protocol.

Opt-in bounded pilot: a typed committed DomainEvent is the only publishable
cause; ACK atomically stages one Command. ACK is NOT a certified business
effect and no terminal certificate is created by this adapter.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

from sose.composition.boundary import (
    BoundaryConsumerRegistry,
    BoundaryLease,
    BoundaryMessage,
    BoundaryService,
)
from sose.core.events import Command, DomainEvent
from sose.core.identity import deterministic_id
from sose.persistence.base import Persistence, UnitOfWork

from .od5a_durable import _event_id, _key, _time_us


_KIND = "od5a_durable_pilot"
_SOURCE = "od5a_durable"
_DESTINATION = "od5a_command_staging"
_CONTRACT = "od5a.physical_completion"
_VERSION = 1
_ORIGIN = datetime(2000, 1, 1, tzinfo=timezone.utc)
_PHYSICAL_KINDS = frozenset({"execution", "coordination"})


def _physical_event(
    uow: UnitOfWork, *, job_id: str, event_id: str,
) -> tuple[DomainEvent, dict[str, Any]]:
    entity = uow.get_entity(_KIND, job_id)
    if entity is None:
        raise KeyError(f"unknown durable OD-5a organization: {job_id}")
    event = uow.get_event(event_id)
    if event is None:
        raise ValueError("OD-5a physical publication requires committed domain event")
    payload = event.payload
    if (
        event.entity_type != _KIND
        or event.entity_id != job_id
        or event.correlation_id != job_id
        or event.name not in {f"od5a.{k}.v1" for k in _PHYSICAL_KINDS}
        or payload.get("kind") not in _PHYSICAL_KINDS
        or event.name != f"od5a.{payload['kind']}.v1"
        or payload.get("model_spec_hash") != entity.attributes["model_spec_hash"]
        or payload.get("config_epoch") != 0
        or payload.get("resource") not in entity.attributes["resources"]
        or event.tick is None
    ):
        raise ValueError("OD-5a physical event fails causal or model identity checks")
    resource = payload["resource"]
    expected_prefix = (
        "execution:" if payload["kind"] == "execution" else "coordination:"
    )
    if not resource.startswith(expected_prefix):
        raise ValueError("OD-5a physical resource class does not match event kind")
    start, end = (payload.get("start"), payload.get("end"))
    if not all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in (start, end)):
        raise ValueError("OD-5a physical event times must be numeric")
    start_us = Decimal(str(start)) * 1_000_000
    end_us = Decimal(str(end)) * 1_000_000
    if (
        start_us != start_us.to_integral_value()
        or end_us != end_us.to_integral_value()
        or start_us > end_us
        or event.occurred_at != _ORIGIN + timedelta(microseconds=int(end_us))
    ):
        raise ValueError("OD-5a physical event time contradicts durable clock")
    # A durable row alone cannot attest that the scheduler performed work.
    # Require the deterministic _emit identity and its committed stage witness.
    state = entity.attributes
    item_id, source, target = (
        payload.get("item_id"), payload.get("source"), payload.get("target")
    )
    if (
        not all(isinstance(x, str) and x for x in (item_id, source, target))
        or item_id not in state["services"]
        or source not in state["stations"]
        or target not in state["stations"]
    ):
        raise ValueError("OD-5a physical event refers to unadmitted item or station")
    if event.event_id != _event_id(
        job_id, payload["kind"], item_id, source, target,
    ):
        raise ValueError("OD-5a physical event canonical identity mismatch")
    if (
        isinstance(event.tick, bool)
        or not isinstance(event.tick, int)
        or not (0 < event.tick <= state["step"])
        or event.occurred_at > _ORIGIN + timedelta(
            microseconds=_time_us(state["clock"], "durable clock"),
        )
    ):
        raise ValueError("OD-5a physical event exceeds durable scheduler clock")
    if payload["kind"] == "execution":
        if (
            source != target
            or _key(item_id, target) not in state["finished"]
            or end_us - start_us != _time_us(
                state["services"][item_id][target], "service duration",
            )
        ):
            raise ValueError("OD-5a execution event has no authoritative finished trail")
    else:
        incoming = state["incoming"][target]
        received = state["received"].get(_key(item_id, target), {})
        if (
            source == target or source not in incoming
            or received.get(source) != event.event_id
            or end_us - start_us != _time_us(
                state["coordination_latency"], "coordination latency",
            )
        ):
            raise ValueError("OD-5a coordination event has no authoritative received trail")
    causes = payload.get("causes")
    if not isinstance(causes, list) or not all(
        isinstance(parent, str) and parent for parent in causes
    ) or len(causes) != len(set(causes)):
        raise ValueError("OD-5a physical event has invalid causal parent trail")
    if event.causation_id != (sorted(causes)[-1] if causes else None):
        raise ValueError("OD-5a physical event causal parent identity mismatch")
    for parent_id in causes:
        parent = uow.get_event(parent_id)
        if (
            parent is None or parent.entity_type != _KIND
            or parent.entity_id != job_id or parent.correlation_id != job_id
            or parent.occurred_at > event.occurred_at
            or parent.tick is None or parent.tick >= event.tick
        ):
            raise ValueError("OD-5a physical event has missing or future causal parent")
    return event, {
        "event_id": event.event_id,
        "model_spec_hash": entity.attributes["model_spec_hash"],
        "admission_identity": entity.attributes["identity"],
        "config_epoch": 0,
        "kind": payload["kind"],
        "item_id": payload["item_id"],
        "source": payload["source"],
        "target": payload["target"],
        "resource": payload["resource"],
        "start_us": int(start_us),
        "end_us": int(end_us),
        "tick": event.tick,
        "causes": list(payload["causes"]),
    }


def publish_od5a_physical_completion(
    store: Persistence, *, job_id: str, event_id: str,
):
    """Idempotently publish a committed physically occupied OD-5a interval."""
    with store.transaction() as uow:
        event, payload = _physical_event(uow, job_id=job_id, event_id=event_id)
    message = BoundaryMessage.create(
        contract_name=_CONTRACT, contract_version=_VERSION,
        source_domain=_SOURCE, source_identity=job_id,
        destination_domain=_DESTINATION,
        occurrence_key=event.event_id, correlation_id=job_id,
        causation_id=event.event_id, causation_kind="event",
        produced_at=event.occurred_at, payload=payload,
    )
    return BoundaryService(store).publish(message)


def claim_od5a_physical_completion(
    store: Persistence, *, job_id: str, owner_id: str,
    now: datetime, lease_duration: timedelta,
) -> BoundaryLease | None:
    """Claim only this organization's pending physical ingress messages."""
    return BoundaryService(store).claim_next(
        owner_id=owner_id, now=now, lease_duration=lease_duration,
        destination_domain=_DESTINATION,
        accepted_contracts=frozenset({(_CONTRACT, _VERSION)}),
        correlation_id=job_id,
    )


def _stage_physical_command(
    message: BoundaryMessage, uow: UnitOfWork,
) -> str:
    if message.causation_kind != "event":
        raise ValueError("physical ACK requires typed committed event cause")
    event, expected = _physical_event(
        uow, job_id=message.correlation_id, event_id=message.causation_id,
    )
    if (
        message.source_domain != _SOURCE
        or message.source_identity != message.correlation_id
        or message.occurrence_key != event.event_id
        or message.produced_at != event.occurred_at
        or message.payload() != expected
    ):
        raise ValueError("physical ACK payload diverges from durable causal event")
    effect_id = deterministic_id("od5a-physical-command", message.message_id)
    command = Command(
        command_id=effect_id,
        name="od5a.stage_physical_completion.v1",
        entity_type="od5a_physical_observation",
        entity_id=event.event_id,
        due_at=message.produced_at,
        issued_at=message.produced_at,
        payload={
            "event_id": event.event_id,
            "model_spec_hash": expected["model_spec_hash"],
            "admission_identity": expected["admission_identity"],
            "config_epoch": 0,
        },
        causation_id=message.message_id,
        correlation_id=message.correlation_id,
    )
    existing = uow.get_command(effect_id)
    if existing is not None and existing != command:
        raise ValueError("OD-5a staged physical command identity conflict")
    if existing is None:
        uow.save_command(command)
    return effect_id


def consume_od5a_physical_completion(
    store: Persistence, *, lease: BoundaryLease, now: datetime,
):
    """Atomically ACK and stage Command. No business certificate is created."""
    registry = BoundaryConsumerRegistry()
    registry.register(
        destination_domain=_DESTINATION, contract_name=_CONTRACT,
        contract_version=_VERSION, handler=_stage_physical_command,
    )
    return BoundaryService(store).consume(lease=lease, registry=registry, now=now)

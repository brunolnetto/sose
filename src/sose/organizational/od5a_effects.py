"""Event-keyed downstream OD-5a observation; ACK is not certification.

The committed operational physical DomainEvent stays the source of truth.
Only an independently materialized, versioned terminal observation can be
certified by the existing BusinessEffectService in a distinct transaction.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from sose.composition.model import BoundaryDelivery, BoundaryMessage, DeliveryStatus
from sose.core.events import DomainEvent
from sose.core.identity import deterministic_id
from sose.domain.entity import Entity
from sose.persistence.base import Persistence, UnitOfWork

from .od5a_boundary import _physical_event


_OBSERVATION_KIND = "od5a_physical_observation"
_COMMAND_NAME = "od5a.stage_physical_completion.v1"
_CONTRACT_KEY = "od5a.physical_completion.v1"
_SOURCE_DOMAIN = "od5a_durable"
_DESTINATION_DOMAIN = "od5a_command_staging"


def _transaction(store: Persistence):
    factory = getattr(store, "boundary_transaction", None)
    return factory() if callable(factory) else store.transaction()


def _applied_event_id(effect_id: str) -> str:
    return deterministic_id("od5a-physical-observation-applied", effect_id)


def _require_consumed_command(uow: UnitOfWork, *, effect_id: str):
    command = uow.get_command(effect_id)
    if command is None:
        raise ValueError("OD-5a observation requires a pending Command")
    if (
        command.name != _COMMAND_NAME
        or command.entity_type != _OBSERVATION_KIND
        or not command.entity_id
        or not command.correlation_id
        or not command.causation_id
    ):
        raise ValueError("OD-5a observation Command identity or target is invalid")
    message = uow.get_boundary_message(command.causation_id)
    if (
        message is None
        or message.contract_key != _CONTRACT_KEY
        or message.source_domain != _SOURCE_DOMAIN
        or message.destination_domain != _DESTINATION_DOMAIN
        or message.correlation_id != command.correlation_id
        or message.causation_kind != "event"
        or message.causation_id != command.entity_id
    ):
        raise ValueError("OD-5a observation Command correlation or source is invalid")
    receipt = BoundaryDelivery.pending(message)
    delivery = uow.get_boundary_delivery(receipt.delivery_id)
    ack = uow.get_boundary_consumption(receipt.delivery_id)
    if (
        delivery is None or ack is None
        or delivery.status is not DeliveryStatus.CONSUMED
        or delivery.consumer_effect_id != effect_id
        or ack.consumer_effect_id != effect_id
        or ack.message_id != message.message_id
    ):
        raise ValueError("OD-5a observation requires durable accepted ACK")
    event, payload = _physical_event(
        uow, job_id=message.correlation_id, event_id=command.entity_id,
    )
    if (
        message.payload() != payload
        or message.produced_at != event.occurred_at
        or command.payload.get("event_id") != event.event_id
        or command.payload.get("model_spec_hash") != payload["model_spec_hash"]
        or command.payload.get("admission_identity") != payload["admission_identity"]
        or command.payload.get("config_epoch") != 0
        or command.due_at != message.produced_at
    ):
        raise ValueError("OD-5a observation Command contradicts physical source event")
    return command, message, event, payload


def _attributes(
    *, effect_id: str, message: BoundaryMessage,
    event: DomainEvent, payload: dict[str, Any],
) -> dict[str, Any]:
    return {
        "source_event_id": event.event_id,
        "correlation_id": message.correlation_id,
        "boundary_message_id": message.message_id,
        "model_spec_hash": payload["model_spec_hash"],
        "admission_identity": payload["admission_identity"],
        "config_epoch": 0,
        "resource": payload["resource"],
        "start_us": payload["start_us"],
        "end_us": payload["end_us"],
        "kind": payload["kind"],
        "applied_effect_id": effect_id,
    }


def _verify_terminal(
    uow: UnitOfWork, *, effect_id: str, entity: Entity,
    message: BoundaryMessage, event: DomainEvent, payload: dict[str, Any],
) -> None:
    if (
        entity.id != event.event_id
        or entity.entity_type != _OBSERVATION_KIND
        or entity.state != "applied"
        or entity.version < 1
        or entity.attributes != _attributes(
            effect_id=effect_id, message=message, event=event, payload=payload,
        )
    ):
        raise ValueError("OD-5a applied observation contradicts physical source state")
    proof = uow.get_event(_applied_event_id(effect_id))
    if (
        proof is None or proof.name != "od5a.observation_applied.v1"
        or proof.entity_type != _OBSERVATION_KIND
        or proof.entity_id != event.event_id
        or proof.correlation_id != message.correlation_id
        or proof.causation_id != event.event_id
        or proof.occurred_at < event.occurred_at
        or proof.payload.get("boundary_message_id") != message.message_id
        or proof.payload.get("model_spec_hash") != payload["model_spec_hash"]
    ):
        raise ValueError("OD-5a applied observation lacks immutable causal event proof")


def apply_od5a_physical_observation(
    store: Persistence, *, effect_id: str, applied_at: datetime,
) -> Entity:
    """Materialize once; a crash after commit can replay without new effects."""
    with _transaction(store) as uow:
        command, message, physical, physical_payload = _require_consumed_command(
            uow, effect_id=effect_id,
        )
        if applied_at < physical.occurred_at:
            raise ValueError("OD-5a applied observation cannot predate physical completion")
        existing = uow.get_entity(_OBSERVATION_KIND, physical.event_id)
        if existing is not None:
            _verify_terminal(
                uow, effect_id=effect_id, entity=existing, message=message,
                event=physical, payload=physical_payload,
            )
            return existing
        entity = Entity(
            id=physical.event_id,
            entity_type=_OBSERVATION_KIND,
            state="applied",
            attributes=_attributes(
                effect_id=effect_id, message=message,
                event=physical, payload=physical_payload,
            ),
            created_at=applied_at,
            updated_at=applied_at,
        )
        entity.touch(applied_at)
        uow.save_entity(entity)
        uow.append_event(DomainEvent(
            event_id=_applied_event_id(effect_id),
            name="od5a.observation_applied.v1",
            entity_type=_OBSERVATION_KIND,
            entity_id=physical.event_id,
            occurred_at=applied_at,
            causation_id=physical.event_id,
            correlation_id=message.correlation_id,
            payload={
                "boundary_message_id": message.message_id,
                "model_spec_hash": physical_payload["model_spec_hash"],
                "config_epoch": 0,
                "source_event_id": physical.event_id,
            },
        ))
        return entity


def verify_od5a_applied_proof(
    uow: UnitOfWork, *, effect_id: str,
    message: BoundaryMessage, entity: Entity,
) -> None:
    """Additional fail-closed predicate called before certifying this one intent."""
    command, validated_message, event, payload = _require_consumed_command(
        uow, effect_id=effect_id,
    )
    if validated_message != message or command.entity_id != entity.id:
        raise ValueError("OD-5a effect certificate mismatches accepted causal Command")
    _verify_terminal(
        uow, effect_id=effect_id, entity=entity,
        message=message, event=event, payload=payload,
    )

"""Event-keyed OD-5a downstream observation and independently certified effect.

The upstream physical DomainEvent is authoritative operational truth.
BoundaryConsumption is transport acceptance; a Command is durable intent.
An immutable versioned observation is downstream domain truth, and only
then may its separate certificate be issued. No SimPy state is persisted.

This bounded adapter does not admit OD-5a confirmatory scientific execution.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from sose.composition.effects import BusinessEffectApplied
from sose.composition.model import (
    BoundaryConsumption, BoundaryDelivery, BoundaryMessage, DeliveryStatus,
)
from sose.core.events import Command, DomainEvent
from sose.core.identity import deterministic_id
from sose.domain.entity import Entity
from sose.persistence.base import Persistence, UnitOfWork

from .od5a_boundary import _physical_event


_OBSERVATION_TYPE = "od5a_physical_observation"
_TERMINAL_STATE = "observed"
_INTENT_NAME = "od5a.stage_physical_completion.v1"
_CONTRACT = "od5a.physical_completion.v1"
_SOURCE = "od5a_durable"
_DESTINATION = "od5a_command_staging"


def _transaction(store: Persistence):
    """Use the same causal serial transaction as boundary and PC6 receipts."""
    factory = getattr(store, "boundary_transaction", None)
    return factory() if callable(factory) else store.transaction()


def _verify_receipt(
    uow: UnitOfWork, *, message_id: str, effect_id: str,
) -> tuple[DomainEvent, dict[str, Any], str, datetime]:
    message = uow.get_boundary_message(message_id)
    if message is None:
        raise ValueError("OD-5a certification lacks durable source boundary message")
    if (
        message.contract_key != _CONTRACT
        or message.source_domain != _SOURCE
        or message.destination_domain != _DESTINATION
        or message.source_identity != message.correlation_id
        or message.causation_kind != "event"
        or not message.causation_id
    ):
        raise ValueError("OD-5a certification requires registered physical contract and typed cause")
    canonical = BoundaryMessage.create(
        contract_name=message.contract_name,
        contract_version=message.contract_version,
        source_domain=message.source_domain,
        source_identity=message.source_identity,
        destination_domain=message.destination_domain,
        occurrence_key=message.occurrence_key,
        correlation_id=message.correlation_id,
        causation_id=message.causation_id,
        causation_kind=message.causation_kind,
        produced_at=message.produced_at,
        payload=message.payload(),
    )
    if message != canonical:
        raise ValueError("OD-5a boundary message has noncanonical deterministic identity")
    event, expected_payload = _physical_event(
        uow, job_id=message.correlation_id, event_id=message.causation_id,
    )
    if (
        message.occurrence_key != event.event_id
        or message.produced_at != event.occurred_at
        or message.payload() != expected_payload
        or effect_id != deterministic_id("od5a-physical-command", message.message_id)
    ):
        raise ValueError("OD-5a certified event identity or boundary payload mismatch")

    delivery_id = BoundaryDelivery.pending(message).delivery_id
    delivery = uow.get_boundary_delivery(delivery_id)
    consumption = uow.get_boundary_consumption(delivery_id)
    if (
        delivery is None or delivery.status is not DeliveryStatus.CONSUMED
        or delivery.message_id != message.message_id
        or delivery.destination_domain != message.destination_domain
        or delivery.contract_name != message.contract_name
        or delivery.contract_version != message.contract_version
        or not delivery.claim_owner_id
        or delivery.claim_epoch <= 0
        or delivery.attempts != delivery.claim_epoch
        or delivery.consumer_effect_id != effect_id
        or consumption is None
        or consumption.delivery_id != delivery_id
        or consumption.message_id != message.message_id
        or consumption.consumer_effect_id != effect_id
        or consumption.destination_domain != _DESTINATION
        or consumption.contract_name != message.contract_name
        or consumption.contract_version != message.contract_version
        or delivery.consumed_at != consumption.consumed_at
        or delivery.claimed_at is None
        or delivery.claimed_at > consumption.consumed_at
        or delivery.claimed_at < event.occurred_at
        or delivery.lease_expires_at is None
        or delivery.lease_expires_at <= consumption.consumed_at
        or consumption.consumed_at < event.occurred_at
        or consumption != BoundaryConsumption.create(
            delivery=delivery,
            consumer_effect_id=effect_id,
            consumed_at=consumption.consumed_at,
        )
    ):
        raise ValueError("OD-5a physical effect lacks unique accepted boundary ACK")
    return event, expected_payload, message.correlation_id, consumption.consumed_at


def _verified_intent(
    uow: UnitOfWork, *, effect_id: str,
) -> tuple[Command, DomainEvent, dict[str, Any], dict[str, Any], datetime]:
    command = uow.get_command(effect_id)
    if command is None:
        raise ValueError("OD-5a physical effect lacks pending Command")
    if (
        command.name != _INTENT_NAME
        or command.entity_type != _OBSERVATION_TYPE
        or not command.causation_id
        or not command.correlation_id
    ):
        raise ValueError("OD-5a physical effect has incorrect staged Command")
    event, physical, job_id, ack_at = _verify_receipt(
        uow, message_id=command.causation_id, effect_id=effect_id,
    )
    expected_command_payload = {
        "event_id": event.event_id,
        "model_spec_hash": physical["model_spec_hash"],
        "admission_identity": physical["admission_identity"],
        "config_epoch": physical["config_epoch"],
    }
    if (
        command.command_id != effect_id
        or command.entity_id != event.event_id
        or command.correlation_id != job_id
        or command.due_at != event.occurred_at
        or command.issued_at != event.occurred_at
        or command.payload != expected_command_payload
    ):
        raise ValueError("OD-5a physical staged Command contradicts event or organization")
    attributes = {
        **physical,
        "source_event_id": event.event_id,
        "boundary_message_id": command.causation_id,
        "effect_id": effect_id,
        "correlation_id": job_id,
    }
    return command, event, physical, attributes, ack_at


def _verified_observation(
    uow: UnitOfWork, *, event: DomainEvent, attributes: dict[str, Any],
    ack_at: datetime,
) -> Entity:
    entity = uow.get_entity(_OBSERVATION_TYPE, event.event_id)
    if (
        entity is None
        or entity.id != event.event_id
        or entity.entity_type != _OBSERVATION_TYPE
        or entity.state != _TERMINAL_STATE
        or entity.version != 1
        or entity.created_at != event.occurred_at
        or entity.updated_at is None
        or entity.updated_at < entity.created_at
        or entity.updated_at < ack_at
        or entity.attributes != attributes
    ):
        raise ValueError("OD-5a physical observation lacks exact terminal authoritative truth")
    return entity


def _verified_prior_certificate(
    uow: UnitOfWork, *, effect_id: str, prior: BusinessEffectApplied,
) -> Entity:
    if uow.get_command(effect_id) is not None:
        raise ValueError("certified OD-5a effect still owns a pending Command")
    event, physical, correlation_id, ack_at = _verify_receipt(
        uow, message_id=prior.boundary_message_id, effect_id=effect_id,
    )
    attributes = {
        **physical,
        "source_event_id": event.event_id,
        "boundary_message_id": prior.boundary_message_id,
        "effect_id": effect_id,
        "correlation_id": correlation_id,
    }
    entity = _verified_observation(
        uow, event=event, attributes=attributes, ack_at=ack_at,
    )
    expected = BusinessEffectApplied.from_applied(
        effect_id=effect_id,
        boundary_message_id=prior.boundary_message_id,
        correlation_id=correlation_id,
        entity_type=_OBSERVATION_TYPE,
        entity_id=event.event_id,
        terminal_state=_TERMINAL_STATE,
        entity_version=entity.version,
        completed_at=prior.completed_at,
    )
    if (
        prior != expected
        or prior.completed_at < event.occurred_at
        or prior.completed_at < ack_at
        or prior.completed_at < entity.updated_at
    ):
        raise ValueError("OD-5a certificate contradicts authoritative observation")
    return entity


def apply_od5a_physical_observation(
    store: Persistence, *, effect_id: str, applied_at: datetime,
) -> Entity:
    """Materialize one terminal event-keyed observation, *without* certifying.

    This is an independently committed idempotent downstream domain step;
    death after COMMIT and before caller response cannot duplicate it.
    """
    with _transaction(store) as uow:
        prior = uow.get_business_effect(effect_id)
        if prior is not None:
            return _verified_prior_certificate(uow, effect_id=effect_id, prior=prior)
        _, event, _, attributes, ack_at = _verified_intent(uow, effect_id=effect_id)
        existing = uow.get_entity(_OBSERVATION_TYPE, event.event_id)
        if existing is not None:
            return _verified_observation(
                uow, event=event, attributes=attributes, ack_at=ack_at,
            )
        if applied_at < event.occurred_at or applied_at < ack_at:
            raise ValueError("OD-5a physical observation cannot precede its boundary ACK")
        entity = Entity(
            id=event.event_id,
            entity_type=_OBSERVATION_TYPE,
            state=_TERMINAL_STATE,
            attributes=attributes,
            created_at=event.occurred_at,
        )
        entity.touch(applied_at)
        uow.save_entity(entity)
        return entity


def certify_od5a_physical_observation(
    store: Persistence, *, effect_id: str, completed_at: datetime,
) -> BusinessEffectApplied:
    """Certify only an *already committed*, versioned terminal observation.

    Certificate insertion and staged Command deletion share one authoritative
    UoW, so an interrupted attempt leaves either both or neither committed.
    """
    with _transaction(store) as uow:
        prior = uow.get_business_effect(effect_id)
        if prior is not None:
            _verified_prior_certificate(uow, effect_id=effect_id, prior=prior)
            return prior
        command, event, _, attributes, ack_at = _verified_intent(
            uow, effect_id=effect_id,
        )
        entity = _verified_observation(
            uow, event=event, attributes=attributes, ack_at=ack_at,
        )
        if (
            completed_at < event.occurred_at
            or completed_at < ack_at
            or completed_at < entity.updated_at
        ):
            raise ValueError("OD-5a physical certificate precedes terminal observation")
        certificate = BusinessEffectApplied.from_applied(
            effect_id=effect_id,
            boundary_message_id=command.causation_id,
            correlation_id=command.correlation_id,
            entity_type=entity.entity_type,
            entity_id=entity.id,
            terminal_state=entity.state,
            entity_version=entity.version,
            completed_at=completed_at,
        )
        uow.save_business_effect(certificate)
        uow.delete_command(effect_id)
        return certificate


__all__ = [
    "apply_od5a_physical_observation",
    "certify_od5a_physical_observation",
]

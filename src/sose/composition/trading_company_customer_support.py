"""Shared primitives of the Trading Company customer path, split from trading_company_customer."""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta
from collections.abc import Mapping

from sose.core.events import Command
from sose.core.identity import deterministic_id
from sose.core.resource_reservations import TemporalReservation
from sose.persistence.memory import MemoryPersistence

from sose.examples.order_to_cash import simulation as o2c
from sose.examples.warehouse_fulfillment import simulation as fulfillment
from sose.examples.logistics import simulation as logistics
from sose.examples.cards_payments import simulation as payments
from sose.examples.record_to_report import simulation as r2r
from sose.examples.warehouse_management import simulation as wm

from .boundary import BoundaryConsumerRegistry, BoundaryService
from .effects import BusinessEffectService, CERTIFIED_INTENTS
from .model import BoundaryMessage, DeliveryStatus


class DurableResourceWait(RuntimeError):
    """Durable work remains pending because finite physical capacity is busy.

    Not a failed business transition: an independent recurring worker must
    retry the original Command after competing physical work is released.
    """

    def __init__(self, resource_name: str, request_id: str) -> None:
        self.resource_name = resource_name
        self.request_id = request_id
        super().__init__(f"physical resource still pending: {resource_name} / {request_id}")



def _raise_delivery_dispatch_failure_or_wait(
    persistence: MemoryPersistence, *, shipment_id: str, ordinal: int,
) -> None:
    """Distinguish a genuine finite courier wait from an unexplained false.

    The exact request must still be an ungranted, durable delivery-courier
    demand. In particular, a granted reservation or an unrelated business
    failure must never be reclassified as transient contention.
    """
    attempt_id = logistics.delivery_attempt_id(ordinal, shipment_id=shipment_id)
    request_id = f"delivery-courier:{attempt_id}"
    pending = any(
        demand.request_id == request_id and demand.resource_name == "delivery_courier"
        for demand in persistence.resource_demands()
    )
    granted = any(
        reservation.request_id == request_id
        for reservation in persistence.resource_reservations()
    )
    if pending and not granted:
        raise DurableResourceWait("delivery_courier", request_id)
    raise RuntimeError("logistics delivery dispatch failed")


@dataclass(frozen=True, slots=True)
class _CustomerFixtures:
    o2c: o2c.O2CEntities
    fulfillment: fulfillment.WarehouseEntities
    warehouse: wm.WarehouseManagementEntities
    logistics: logistics.LogisticsEntities
    payments: payments.PaymentEntities
    r2r: r2r.R2REntities


@dataclass(frozen=True, slots=True)
class _IntentExecution:
    """Arguments of one `_execute_intent` call, handed to the per-intent executor."""

    persistence: MemoryPersistence
    intent: Command
    effect_id: str
    fixtures: _CustomerFixtures
    correlation_id: str
    organization_id: str
    authoritative_pickup: TemporalReservation | None
    resource_persistence: MemoryPersistence | None


def _next_logical_time(
    persistence: MemoryPersistence,
    previous: datetime,
    *,
    organization_id: str,
) -> datetime:
    candidate = previous + timedelta(minutes=1)
    position = persistence.simulation_position(owner_id=organization_id)
    if position is not None and position.logical_time > candidate:
        return position.logical_time
    return candidate


def _publish(
    service: BoundaryService,
    *,
    contract_name: str,
    source_domain: str,
    source_identity: str,
    destination_domain: str,
    occurrence_key: str,
    correlation_id: str,
    causation_id: str | None,
    produced_at: datetime,
    payload: dict[str, object],
    contract_version: int = 1,
    recovery_job_id: str | None = None,
) -> BoundaryMessage:
    message = BoundaryMessage.create(
        contract_name=contract_name,
        contract_version=contract_version,
        source_domain=source_domain,
        source_identity=source_identity,
        destination_domain=destination_domain,
        occurrence_key=occurrence_key,
        correlation_id=correlation_id,
        causation_id=causation_id,
        produced_at=produced_at,
        payload=payload,
    )
    service.publish(message, recovery_job_id=recovery_job_id)
    return message


class _CausalCommandFactory:
    def __init__(
        self,
        delegate,
        *,
        correlation_id: str,
        caused_by: Command,
    ) -> None:
        self._delegate = delegate
        self._correlation_id = correlation_id
        self._caused_by = caused_by

    def create(self, name: str, **kwargs):
        kwargs["correlation_id"] = self._correlation_id
        kwargs["caused_by"] = self._caused_by
        return self._delegate.create(name, **kwargs)


@contextmanager
def _causal_command_scope(engine, *, intent: Command, correlation_id: str):
    original = engine.context.commands
    engine.context.commands = _CausalCommandFactory(
        original,
        correlation_id=correlation_id,
        caused_by=intent,
    )
    try:
        yield
    finally:
        engine.context.commands = original


def _recovery_time(
    persistence: MemoryPersistence,
    requested_at: datetime,
    *,
    organization_id: str,
) -> datetime:
    position = persistence.simulation_position(owner_id=organization_id)
    if position is None:
        return requested_at
    if requested_at < position.logical_time:
        raise ValueError("composition intent cannot precede committed logical time")
    return position.logical_time


def _dispatch_o2c_event(
    persistence: MemoryPersistence,
    engine,
    *,
    order_id: str,
    event: str,
    correlation_id: str,
    causation_id: str,
) -> None:
    order = persistence.entity("sales_order", order_id)
    if order is None:
        raise RuntimeError(f"missing sales order: {order_id}")
    command = engine.context.commands.create(
        event,
        target=order,
        correlation_id=correlation_id,
        key=("trading-company", order.id, event, causation_id),
    )
    command = Command(
        command_id=command.command_id,
        name=command.name,
        entity_type=command.entity_type,
        entity_id=command.entity_id,
        due_at=command.due_at,
        issued_at=command.issued_at,
        tick=command.tick,
        payload=command.payload,
        causation_id=causation_id,
        correlation_id=correlation_id,
        owner_id=command.owner_id,
    )
    engine.dispatch(command)


def _operational_owner(organization_owners: Mapping[str, str], correlation_id: str) -> str:
    """Resolve an explicit durable binding; correlation never becomes ownership."""
    if not isinstance(correlation_id, str) or not correlation_id.strip():
        raise ValueError("egress correlation_id must not be empty")
    owner_id = organization_owners.get(correlation_id)
    if not isinstance(owner_id, str) or not owner_id.strip():
        raise ValueError("egress requires explicit correlation-to-organization ownership")
    return owner_id


def _is_foreign_or_unconsumed(
    delivery, message: BoundaryMessage | None, *, contract_key: str, correlation_id: str | None,
) -> bool:
    """True when an inbound delivery is not a consumed message of the reconciled contract."""
    return (
        message is None
        or message.contract_key != contract_key
        or (correlation_id is not None and message.correlation_id != correlation_id)
        or delivery.status is not DeliveryStatus.CONSUMED
    )


def _consumer_effect_is_pending(uow, delivery, *, missing_receipt: str) -> bool:
    """True while the Command accepted by a consumed delivery has not been executed."""
    consumption = uow.get_boundary_consumption(delivery.delivery_id)
    if consumption is None:
        raise RuntimeError(missing_receipt)
    return uow.get_command(consumption.consumer_effect_id) is not None


def _complete_intent(
    persistence: MemoryPersistence, intent: Command, *, effect_id: str, organization_id: str,
) -> None:
    if intent.name in CERTIFIED_INTENTS:
        # Completion requires authoritative terminal state and a durable ACK
        # receipt; proof insert and staged Command deletion share one UoW.
        # A crash after business transition but before this commit retries
        # idempotently instead of silently asserting an ACK is an effect.
        position = persistence.simulation_position(owner_id=organization_id)
        completed_at = max(
            intent.due_at,
            position.logical_time if position is not None else intent.due_at,
        )
        BusinessEffectService(persistence).complete(
            effect_id=effect_id, completed_at=completed_at,
        )
    else:
        with persistence.transaction() as uow:
            current = uow.get_command(effect_id)
            if current == intent:
                uow.delete_command(effect_id)


def _intent_handler(
    *,
    intent_name: str,
    entity_type: str,
    entity_id: str,
    owner_id: str,
):
    if not isinstance(owner_id, str) or not owner_id.strip():
        raise ValueError("composition intent requires operational owner_id")

    def handle(message: BoundaryMessage, uow) -> str:
        command_id = deterministic_id(
            "composition-domain-intent",
            message.message_id,
            intent_name,
            entity_type,
            entity_id,
        )
        command = Command(
            command_id=command_id,
            name=intent_name,
            entity_type=entity_type,
            entity_id=entity_id,
            due_at=message.produced_at,
            issued_at=message.produced_at,
            payload={
                **message.payload(),
                "boundary_message_id": message.message_id,
                "boundary_contract": message.contract_key,
            },
            causation_id=message.message_id,
            correlation_id=message.correlation_id,
            owner_id=owner_id,
        )
        existing = uow.get_command(command_id)
        if existing is not None and existing != command:
            raise ValueError(f"composition intent identity conflict: {command_id}")
        if existing is None:
            uow.save_command(command)
        return command_id

    return handle


def _consume_next(
    service: BoundaryService,
    registry: BoundaryConsumerRegistry,
    *,
    owner_id: str,
    now: datetime,
    expected_message_id: str,
) -> str:
    lease = service.claim_next(
        owner_id=owner_id,
        now=now,
        lease_duration=timedelta(hours=1),
        message_id=expected_message_id,
    )
    if lease is None:
        raise RuntimeError("expected own pending composition delivery")
    if lease.message_id != expected_message_id:
        raise RuntimeError("composition claimed another causal flow")
    consumption = service.consume(
        lease=lease,
        registry=registry,
        now=now,
    )
    return consumption.consumer_effect_id

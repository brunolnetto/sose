"""Charging of already-committed enrolled work to a PC6 recovery occurrence.

Split from recovery.py (first transaction of ``_run_bounded``).
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Any

from sose.composition.model import DeliveryStatus
from sose.jobs.model import SimulationJobState
from sose.persistence.base import Persistence

if TYPE_CHECKING:
    from sose.composition.recovery import TradingCustomerRecoveryRunner


def _charge_publications(uow: Any, current_job: SimulationJobState) -> int:
    # ACK+Command is one transaction, but is a separate bounded
    # scheduling action from executing that Command. A worker
    # killed after ACK must not apply the domain in the same slot.
    actions = 0
    for message_id in current_job.active_publication_message_ids:
        message = uow.get_boundary_message(message_id)
        deliveries = tuple(
            d for d in uow.boundary_deliveries()
            if d.message_id == message_id
        )
        if message is None or len(deliveries) != 1:
            raise RuntimeError(
                "enrolled recovery publication lacks durable message/delivery"
            )
        if (
            deliveries[0].contract_name != message.contract_name
            or deliveries[0].contract_version != message.contract_version
        ):
            raise RuntimeError(
                "enrolled recovery publication contradicts delivery contract"
            )
        actions += 1
    return actions


def _charge_consumed_ack(uow: Any, message_id: str, delivery: Any) -> None:
    receipt = uow.get_boundary_consumption(delivery.delivery_id)
    if (
        receipt is None
        or receipt.consumer_effect_id != delivery.consumer_effect_id
        or uow.get_boundary_message(message_id) is None
    ):
        raise RuntimeError(
            "enrolled recovery ACK lost its atomic Command receipt"
        )
    if (
        uow.get_command(receipt.consumer_effect_id) is None
        and uow.get_business_effect(receipt.consumer_effect_id) is None
    ):
        raise RuntimeError(
            "recovery ACK has neither pending Command nor certificate"
        )


def _charge_acks(uow: Any, current_job: SimulationJobState) -> int:
    actions = 0
    for message_id in current_job.active_boundary_message_ids:
        matches = tuple(
            delivery for delivery in uow.boundary_deliveries()
            if delivery.message_id == message_id
        )
        if len(matches) != 1:
            raise RuntimeError(
                "enrolled recovery ACK lost its durable delivery"
            )
        delivery = matches[0]
        if delivery.status is DeliveryStatus.CONSUMED:
            _charge_consumed_ack(uow, message_id, delivery)
            actions += 1
        elif uow.get_boundary_consumption(delivery.delivery_id) is not None:
            raise RuntimeError(
                "unconsumed enrolled delivery has a committed ACK"
            )
    return actions


def _charge_effect(uow: Any, effect_id: str) -> int:
    """Return 1 for a certified effect, 0 while its Command is still pending."""
    certificate = uow.get_business_effect(effect_id)
    command = uow.get_command(effect_id)
    if certificate is None:
        if command is None:
            raise RuntimeError(
                "enrolled recovery effect lacks Command and certificate"
            )
        return 0
    if command is not None:
        raise RuntimeError(
            "enrolled recovery effect has certificate and pending Command"
        )
    if certificate.effect_id != effect_id:
        raise RuntimeError("recovery certificate changed effect identity")
    deliveries = tuple(
        d for d in uow.boundary_deliveries()
        if d.message_id == certificate.boundary_message_id
    )
    if len(deliveries) != 1:
        raise RuntimeError("certified recovery effect lost causal boundary")
    consumption = uow.get_boundary_consumption(deliveries[0].delivery_id)
    if (
        consumption is None
        or consumption.consumer_effect_id != effect_id
    ):
        raise RuntimeError("certified recovery effect lacks original ACK")
    return 1


def charge_committed_work(
    runner: TradingCustomerRecoveryRunner, store: Persistence,
) -> tuple[int, str | None]:
    """Count committed enrolled actions; return them with the active trigger id."""
    actions = 0
    # Each admitted resource effect has a durable enrollment in the
    # current scheduled occurrence. A crash after physical release but
    # before the job checkpoint must not make that slot appear empty.
    with store.transaction() as uow:
        current_job = uow.get_job_state(runner.job_id)
        active_trigger = (
            current_job.active_trigger_id if current_job is not None else None
        )
        # An accepted Command can commit its business certificate and then
        # lose the worker *before* the recurring trigger checkpoint. Charge
        # that certified non-resource effect to its original occurrence.
        # Mapped effects are instead charged by the physical ledger below.
        if current_job is not None:
            actions += _charge_publications(uow, current_job)
            actions += _charge_acks(uow, current_job)
            for effect_id in current_job.active_effect_ids:
                actions += _charge_effect(uow, effect_id)
        if actions > runner.max_actions:
            raise RuntimeError("committed recovery actions exceed trigger budget")
    return actions, active_trigger

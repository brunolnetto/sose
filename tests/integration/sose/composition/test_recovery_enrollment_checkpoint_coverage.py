"""#590: fail closed when durable recovery enrollment loses causal evidence.

The authoritative *preflight* in TradingCustomerRecoveryRunner._run_bounded
reads a real MemoryPersistence transaction snapshot. We deliberately inject
corrupt durable records through its UoW to falsify one enrollment invariant
at a time. The runner's constructor-only fencing surface does not certify a
PostgreSQL writer claim; worker/OS-death fencing has separate acceptance gates.
"""
from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone

import pytest

from sose.composition.effects import BusinessEffectApplied
from sose.composition.model import (
    BoundaryConsumption, BoundaryDelivery, BoundaryMessage, DeliveryStatus,
)
from sose.composition.recovery import TradingCustomerRecoveryRunner
from sose.core.events import Command
from sose.jobs.model import SimulationJobState
from sose.persistence.memory import MemoryPersistence


NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)
JOB = "recover:causality:audit"
OWNER = "worker:recovery-audit"
EFFECT = "effect:accepted"


class _ConstructorFencingSurface:
    """Expose only constructor-required methods; no worker lease is claimed."""

    def writer_epoch(self):
        raise AssertionError("fencing is not exercised by this preflight unit")

    def claim_writer(self, *_args, **_kwargs):
        raise AssertionError("fencing is not exercised by this preflight unit")


def _message(n: int = 1):
    return BoundaryMessage.create(
        contract_name="warehouse.inventory_reserved",
        contract_version=1,
        source_domain="warehouse_management",
        source_identity="stock:one",
        destination_domain="warehouse_fulfillment",
        occurrence_key=f"delivery:{n}",
        correlation_id="correlation:one",
        causation_id=None,
        produced_at=NOW,
        payload={"stock_id": "stock:one"},
    )


def _scenario(*, publications=(), acks=(), effects=(), max_actions=1):
    store = MemoryPersistence()
    state = SimulationJobState(
        job_id=JOB,
        domain_name="trading_company_composition",
        config_json="{}",
        config_revision=1,
        status="running",
        initialized=True,
        logical_time=NOW,
        next_tick=0,
        active_trigger_id="trigger:recovery",
        phase="reconcile",
        active_publication_message_ids=tuple(publications),
        active_boundary_message_ids=tuple(acks),
        active_effect_ids=tuple(effects),
    )
    with store.transaction() as uow:
        uow.save_job_state(state)
    runner = TradingCustomerRecoveryRunner(
        persistence=_ConstructorFencingSurface(),
        owner_id=OWNER,
        job_id=JOB,
        max_actions=max_actions,
    )
    return store, runner


def _save_message(store, message, *, delivery=None):
    with store.transaction() as uow:
        uow.save_boundary_message(message)
        if delivery is not None:
            uow.save_boundary_delivery(delivery)


def _save_delivery(store, delivery):
    with store.transaction() as uow:
        uow.save_boundary_delivery(delivery)


def _save_receipt(store, delivery, *, effect=EFFECT):
    receipt = BoundaryConsumption.create(
        delivery=delivery,
        consumer_effect_id=effect,
        consumed_at=NOW,
    )
    with store.transaction() as uow:
        uow.save_boundary_consumption(receipt)
    return receipt


def _save_command(store, effect=EFFECT):
    command = Command(
        command_id=effect,
        name="composition.deliver_shipment",
        entity_type="shipment",
        entity_id="shipment:one",
        due_at=NOW,
        issued_at=NOW,
    )
    with store.transaction() as uow:
        uow.save_command(command)


def _save_certificate(store, *, effect=EFFECT, message_id="message:missing"):
    certificate = BusinessEffectApplied.from_applied(
        effect_id=effect,
        boundary_message_id=message_id,
        correlation_id="correlation:one",
        entity_type="shipment",
        entity_id="shipment:one",
        terminal_state="delivered",
        entity_version=1,
        completed_at=NOW,
    )
    with store.transaction() as uow:
        uow.save_business_effect(certificate)


def _refuses(store, runner, pattern):
    original_job = store.job_state(JOB)
    original_messages = store.boundary_protocol_snapshot()
    original_events = store.events()
    with pytest.raises(RuntimeError, match=pattern):
        runner._run_bounded(store, now=NOW)
    assert store.job_state(JOB) == original_job
    assert store.boundary_protocol_snapshot() == original_messages
    assert store.events() == original_events


@pytest.mark.parametrize("condition", ["missing_message", "missing_delivery", "duplicate_delivery"])
def test_enrolled_publication_requires_exactly_one_atomic_boundary_delivery(condition):
    msg = _message()
    store, runner = _scenario(publications=(msg.message_id,))
    if condition == "missing_delivery":
        _save_message(store, msg)
    elif condition == "duplicate_delivery":
        delivery = BoundaryDelivery.pending(msg)
        _save_message(store, msg, delivery=delivery)
        _save_delivery(store, replace(delivery, delivery_id="delivery:duplicate"))
    _refuses(store, runner, "enrolled recovery publication lacks durable message/delivery")


@pytest.mark.parametrize(
    "changed",
    [{"contract_name": "forged.contract"}, {"contract_version": 99}],
)
def test_enrolled_publication_contract_must_match_immutable_message(changed):
    msg = _message()
    store, runner = _scenario(publications=(msg.message_id,))
    delivery = replace(BoundaryDelivery.pending(msg), **changed)
    _save_message(store, msg, delivery=delivery)
    _refuses(store, runner, "enrolled recovery publication contradicts delivery contract")


@pytest.mark.parametrize("condition", ["missing_delivery", "duplicate_delivery"])
def test_enrolled_boundary_ack_needs_exactly_one_delivery(condition):
    msg = _message()
    store, runner = _scenario(acks=(msg.message_id,))
    if condition == "duplicate_delivery":
        first = BoundaryDelivery.pending(msg)
        _save_message(store, msg, delivery=first)
        _save_delivery(store, replace(first, delivery_id="delivery:forged"))
    _refuses(store, runner, "enrolled recovery ACK lost its durable delivery")


@pytest.mark.parametrize("condition", ["missing_receipt", "wrong_effect", "missing_message"])
def test_consumed_ack_requires_its_exact_atomic_receipt_and_message(condition):
    msg = _message()
    store, runner = _scenario(acks=(msg.message_id,))
    delivery = replace(
        BoundaryDelivery.pending(msg),
        status=DeliveryStatus.CONSUMED,
        consumed_at=NOW,
        consumer_effect_id=EFFECT,
    )
    if condition != "missing_message":
        _save_message(store, msg, delivery=delivery)
    else:
        _save_delivery(store, delivery)
    if condition == "wrong_effect":
        _save_receipt(store, delivery, effect="effect:forged")
    elif condition == "missing_message":
        _save_receipt(store, delivery)
    _refuses(store, runner, "enrolled recovery ACK lost its atomic Command receipt")


def test_consumed_ack_cannot_exist_without_executable_command_or_certificate():
    msg = _message()
    store, runner = _scenario(acks=(msg.message_id,))
    delivery = replace(
        BoundaryDelivery.pending(msg),
        status=DeliveryStatus.CONSUMED,
        consumed_at=NOW,
        consumer_effect_id=EFFECT,
    )
    _save_message(store, msg, delivery=delivery)
    _save_receipt(store, delivery)
    _refuses(store, runner, "recovery ACK has neither pending Command nor certificate")


def test_unconsumed_delivery_cannot_carry_committed_receipt():
    msg = _message()
    store, runner = _scenario(acks=(msg.message_id,))
    delivery = BoundaryDelivery.pending(msg)
    _save_message(store, msg, delivery=delivery)
    _save_receipt(store, delivery)
    _refuses(store, runner, "unconsumed enrolled delivery has a committed ACK")


def test_enrolled_effect_needs_either_executable_command_or_applied_certificate():
    store, runner = _scenario(effects=(EFFECT,))
    _refuses(store, runner, "enrolled recovery effect lacks Command and certificate")


def test_certified_effect_cannot_also_have_an_executable_command():
    store, runner = _scenario(effects=(EFFECT,))
    _save_certificate(store)
    _save_command(store)
    _refuses(store, runner, "enrolled recovery effect has certificate and pending Command")


def test_certified_effect_requires_original_delivery_as_causal_predecessor():
    store, runner = _scenario(effects=(EFFECT,))
    _save_certificate(store)
    _refuses(store, runner, "certified recovery effect lost causal boundary")


@pytest.mark.parametrize("condition", ["missing_receipt", "wrong_effect"])
def test_certified_effect_refuses_missing_or_foreign_ack(condition):
    msg = _message()
    store, runner = _scenario(effects=(EFFECT,))
    delivery = BoundaryDelivery.pending(msg)
    _save_message(store, msg, delivery=delivery)
    _save_certificate(store, message_id=msg.message_id)
    if condition == "wrong_effect":
        _save_receipt(store, delivery, effect="effect:foreign")
    _refuses(store, runner, "certified recovery effect lacks original ACK")


def test_two_committed_publications_cannot_exceed_one_step_budget():
    first, second = _message(1), _message(2)
    store, runner = _scenario(
        publications=(first.message_id, second.message_id), max_actions=1,
    )
    for message in (first, second):
        _save_message(store, message, delivery=BoundaryDelivery.pending(message))
    _refuses(store, runner, "committed recovery actions exceed trigger budget")


def test_single_enrolled_publication_charges_exactly_one_bounded_step_without_republish():
    message = _message()
    store, runner = _scenario(publications=(message.message_id,), max_actions=1)
    _save_message(store, message, delivery=BoundaryDelivery.pending(message))
    original_messages = store.boundary_protocol_snapshot()
    result = runner._run_bounded(store, now=NOW)
    assert result == 1
    assert store.boundary_protocol_snapshot() == original_messages
    assert store.job_state(JOB).active_publication_message_ids == (message.message_id,)

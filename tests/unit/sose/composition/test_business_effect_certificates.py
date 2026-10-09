"""Red-first proof: ACK is not application, and certification is atomic."""
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from sose.composition.boundary import BoundaryConsumerRegistry, BoundaryService
from sose.composition.effects import BusinessEffectApplied, BusinessEffectService
from sose.composition.model import BoundaryMessage
from sose.core.events import Command
from sose.domain.entity import Entity
from sose.persistence.memory import MemoryPersistence
from sose.persistence.sqlite_incremental import SQLiteIncrementalPersistence

NOW = datetime(2026, 10, 9, 12, tzinfo=timezone.utc)


def accepted(store):
    message = BoundaryMessage.create(
        contract_name="warehouse.dispatch_ready", contract_version=1,
        source_domain="warehouse_fulfillment", source_identity="order-1",
        destination_domain="logistics", occurrence_key="dispatch",
        correlation_id="flow-1", causation_id=None, produced_at=NOW,
        payload={"shipment_id": "shipment-1"},
    )
    bus = BoundaryService(store)
    bus.publish(message)
    lease = bus.claim_next(
        owner_id="logistics", now=NOW, lease_duration=timedelta(hours=1),
        destination_domain="logistics",
    )
    registry = BoundaryConsumerRegistry()

    def handler(source, uow):
        command = Command(
            command_id="deliver-shipment-1", name="composition.deliver_shipment",
            entity_type="shipment", entity_id="shipment-1",
            due_at=NOW, causation_id=source.message_id,
            correlation_id=source.correlation_id,
        )
        uow.save_command(command)
        return command.command_id

    registry.register(
        destination_domain="logistics", contract_name="warehouse.dispatch_ready",
        contract_version=1, handler=handler,
    )
    consumption = bus.consume(lease=lease, registry=registry, now=NOW)
    assert consumption.consumer_effect_id == "deliver-shipment-1"
    return message, consumption


def test_ack_is_not_business_effect_and_unapplied_state_cannot_be_certified():
    store = MemoryPersistence()
    message, consumption = accepted(store)
    assert BusinessEffectService(store).get(consumption.consumer_effect_id) is None
    with pytest.raises(ValueError, match="terminal state"):
        BusinessEffectService(store).complete(
            effect_id=consumption.consumer_effect_id, completed_at=NOW,
        )
    assert store.command(consumption.consumer_effect_id) is not None
    with store.transaction() as uow:
        uow.save_entity(Entity(
            id="shipment-1", entity_type="shipment", state="out_for_delivery",
            version=3,
        ))
    with pytest.raises(ValueError, match="terminal state"):
        BusinessEffectService(store).complete(
            effect_id=consumption.consumer_effect_id, completed_at=NOW,
        )
    assert BusinessEffectService(store).get(consumption.consumer_effect_id) is None


def test_verified_delivery_effect_is_one_immutable_certificate_and_deletes_staging():
    store = MemoryPersistence()
    msg, consumed = accepted(store)
    with store.transaction() as uow:
        uow.save_entity(Entity(
            id="shipment-1", entity_type="shipment", state="delivered",
            version=5,
        ))
    cert = BusinessEffectService(store).complete(
        effect_id=consumed.consumer_effect_id, completed_at=NOW,
    )
    assert isinstance(cert, BusinessEffectApplied)
    assert cert.boundary_message_id == msg.message_id
    assert cert.correlation_id == msg.correlation_id
    assert cert.entity_type == "shipment"
    assert cert.entity_id == "shipment-1"
    assert cert.terminal_state == "delivered"
    assert cert.entity_version == 5
    assert store.command(consumed.consumer_effect_id) is None
    assert BusinessEffectService(store).complete(
        effect_id=consumed.consumer_effect_id, completed_at=NOW,
    ) == cert
    assert BusinessEffectService(store).get(consumed.consumer_effect_id) == cert


def test_receipt_rolls_back_together_with_command_deletion_on_commit_failure(monkeypatch):
    store = MemoryPersistence()
    _, consumed = accepted(store)
    with store.transaction() as uow:
        uow.save_entity(Entity(id="shipment-1", entity_type="shipment", state="delivered"))
    original = store.transaction

    from contextlib import contextmanager

    @contextmanager
    def broken_transaction():
        with original() as uow:
            yield uow
            if uow.get_business_effect(consumed.consumer_effect_id) is not None:
                raise OSError("disk full before commit")

    monkeypatch.setattr(store, "transaction", broken_transaction)
    with pytest.raises(OSError, match="disk full"):
        BusinessEffectService(store).complete(
            effect_id=consumed.consumer_effect_id, completed_at=NOW,
        )
    monkeypatch.setattr(store, "transaction", original)
    assert BusinessEffectService(store).get(consumed.consumer_effect_id) is None
    assert store.command(consumed.consumer_effect_id) is not None


def test_certificate_survives_sqlite_restart_without_redelivery(tmp_path):
    path = tmp_path / "business-effect.sqlite"
    with SQLiteIncrementalPersistence(path) as store:
        msg, receipt = accepted(store)
        with store.transaction() as uow:
            uow.save_entity(Entity(
                id="shipment-1", entity_type="shipment", state="delivered", version=8,
            ))
        proof = BusinessEffectService(store).complete(
            effect_id=receipt.consumer_effect_id, completed_at=NOW,
        )
    with SQLiteIncrementalPersistence(path) as store:
        assert BusinessEffectService(store).get(receipt.consumer_effect_id) == proof
        assert BusinessEffectService(store).complete(
            effect_id=receipt.consumer_effect_id, completed_at=NOW,
        ) == proof
        assert store.command(receipt.consumer_effect_id) is None
        assert BoundaryService(store).claim_next(
            owner_id="other", now=NOW + timedelta(days=1),
            lease_duration=timedelta(minutes=5),
        ) is None


def test_corrupted_receipt_or_command_identity_is_rejected():
    store = MemoryPersistence()
    msg, receipt = accepted(store)
    with store.transaction() as uow:
        uow.save_entity(Entity(id="shipment-1", entity_type="shipment", state="delivered"))
        cmd = uow.get_command(receipt.consumer_effect_id)
        uow.save_command(replace(cmd, causation_id="wrong-cause"))
    with pytest.raises(ValueError, match="accepted boundary receipt|causation"):
        BusinessEffectService(store).complete(
            effect_id=receipt.consumer_effect_id, completed_at=NOW,
        )
    assert store.command(receipt.consumer_effect_id) is not None
    assert BusinessEffectService(store).get(receipt.consumer_effect_id) is None


def test_conflicting_recertificate_is_rejected_even_after_command_deletion():
    store = MemoryPersistence()
    _, receipt = accepted(store)
    with store.transaction() as uow:
        uow.save_entity(Entity(id="shipment-1", entity_type="shipment", state="delivered"))
    proof = BusinessEffectService(store).complete(effect_id=receipt.consumer_effect_id, completed_at=NOW)
    with pytest.raises(ValueError, match="conflicts"):
        with store.transaction() as uow:
            uow.save_business_effect(replace(proof, terminal_state="cancelled"))


def test_certificate_cannot_target_other_delivered_shipment_under_same_correlation():
    store = MemoryPersistence()
    source, consumed = accepted(store)
    with store.transaction() as uow:
        uow.save_entity(Entity(id="shipment-1", entity_type="shipment", state="created"))
        uow.save_entity(Entity(id="shipment-2", entity_type="shipment", state="delivered"))
        current = uow.get_command(consumed.consumer_effect_id)
        uow.save_command(replace(current, entity_id="shipment-2"))
    with pytest.raises(ValueError, match="target/contract contradicts causal boundary payload"):
        BusinessEffectService(store).complete(
            effect_id=consumed.consumer_effect_id, completed_at=NOW,
        )
    assert store.command(consumed.consumer_effect_id) is not None
    assert BusinessEffectService(store).get(consumed.consumer_effect_id) is None


@pytest.mark.parametrize("changes", [
    {"source_domain": "untrusted_source"},
    {"destination_domain": "untrusted_destination"},
    {"contract_name": "unrelated.contract"},
])
def test_certificate_fails_closed_on_mismatched_source_contract(changes):
    store = MemoryPersistence()
    source, consumed = accepted(store)
    with store.transaction() as uow:
        uow.save_entity(Entity(id="shipment-1", entity_type="shipment", state="delivered"))
    # Simulate a corrupted imported historical record bypassing immutable insert.
    store._state.boundary_messages[source.message_id] = replace(source, **changes)
    with pytest.raises(ValueError, match="target/contract contradicts causal boundary payload"):
        BusinessEffectService(store).complete(
            effect_id=consumed.consumer_effect_id, completed_at=NOW,
        )
    assert BusinessEffectService(store).get(consumed.consumer_effect_id) is None


def test_independent_sqlite_reader_refreshes_committed_certificate(tmp_path):
    path = tmp_path / "business-effect-cross-instance.sqlite"
    with SQLiteIncrementalPersistence(path) as publisher, \\
         SQLiteIncrementalPersistence(path) as observer:
        _, consumption = accepted(publisher)
        assert observer.business_effect(consumption.consumer_effect_id) is None
        assert observer.business_effects() == ()
        with publisher.transaction() as uow:
            uow.save_entity(Entity(
                id="shipment-1", entity_type="shipment",
                state="delivered", version=4,
            ))
        proof = BusinessEffectService(publisher).complete(
            effect_id=consumption.consumer_effect_id, completed_at=NOW,
        )
        assert observer.business_effect(consumption.consumer_effect_id) == proof
        assert observer.business_effects() == (proof,)

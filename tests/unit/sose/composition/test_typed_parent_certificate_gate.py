"""Typed PC6 descendants require durable business certification, not a missing Command."""
from datetime import datetime, timedelta, timezone

import pytest

from sose.composition.boundary import BoundaryConsumerRegistry, BoundaryService, StaleBoundaryClaimError
from sose.composition.effects import BusinessEffectService
from sose.composition.model import BoundaryMessage
from sose.core.events import Command
from sose.domain.entity import Entity
from sose.persistence.memory import MemoryPersistence
from sose.persistence.sqlite_incremental import SQLiteIncrementalPersistence

NOW = datetime(2026, 10, 9, 12, tzinfo=timezone.utc)


@pytest.mark.parametrize("adapter", ["memory", "sqlite"])
def test_typed_child_cannot_overtake_missing_business_certificate(adapter, tmp_path):
    store = MemoryPersistence() if adapter == "memory" else SQLiteIncrementalPersistence(tmp_path / "typed.sqlite")
    try:
        service = BoundaryService(store)
        parent = BoundaryMessage.create(
            contract_name="warehouse.dispatch_ready", contract_version=1,
            source_domain="warehouse_fulfillment", source_identity="order-1",
            destination_domain="logistics", occurrence_key="parent",
            correlation_id="flow-1", causation_id=None, produced_at=NOW,
            payload={"shipment_id": "shipment-1"},
        )
        child = BoundaryMessage.create(
            contract_name="logistics.delivery_completed", contract_version=1,
            source_domain="logistics", source_identity="shipment-1",
            destination_domain="order_to_cash", occurrence_key="child",
            correlation_id="flow-1", causation_kind="boundary",
            causation_id=parent.message_id, produced_at=NOW + timedelta(seconds=1),
            payload={"order_id": "order-1", "shipment_id": "shipment-1"},
        )
        registry = BoundaryConsumerRegistry()
        def accept(message, uow):
            uow.save_command(Command(
                command_id="deliver-1", name="composition.deliver_shipment",
                entity_type="shipment", entity_id="shipment-1", due_at=NOW,
                causation_id=message.message_id, correlation_id=message.correlation_id,
            ))
            return "deliver-1"
        registry.register(
            destination_domain="logistics", contract_name="warehouse.dispatch_ready",
            contract_version=1, handler=accept,
        )
        service.publish(child)
        service.publish(parent)
        lease = service.claim_next(
            owner_id="logistics", now=NOW, lease_duration=timedelta(hours=1),
            destination_domain="logistics",
        )
        assert lease is not None
        service.consume(lease=lease, registry=registry, now=NOW)
        assert service.claim_next(
            owner_id="o2c", now=NOW, lease_duration=timedelta(hours=1),
            destination_domain="order_to_cash",
        ) is None

        # Simulated legacy/buggy completion: command disappears, no proof.
        with store.transaction() as uow:
            uow.delete_command("deliver-1")
        assert service.claim_next(
            owner_id="o2c", now=NOW, lease_duration=timedelta(hours=1),
            destination_domain="order_to_cash",
        ) is None

        # Restore a pending intent, finish the durable domain state and certify.
        with store.transaction() as uow:
            uow.save_command(Command(
                command_id="deliver-1", name="composition.deliver_shipment",
                entity_type="shipment", entity_id="shipment-1", due_at=NOW,
                causation_id=parent.message_id, correlation_id=parent.correlation_id,
            ))
            uow.save_entity(Entity(
                id="shipment-1", entity_type="shipment", state="delivered", version=2,
            ))
        BusinessEffectService(store).complete(effect_id="deliver-1", completed_at=NOW)
        if adapter == "sqlite":
            store.close()
            store = SQLiteIncrementalPersistence(tmp_path / "typed.sqlite")
            service = BoundaryService(store)
        assert service.claim_next(
            owner_id="o2c", now=NOW, lease_duration=timedelta(hours=1),
            destination_domain="order_to_cash",
        ).message_id == child.message_id
    finally:
        if adapter == "sqlite":
            store.close()

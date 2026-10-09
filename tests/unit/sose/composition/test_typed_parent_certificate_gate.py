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


def test_claimed_typed_child_rechecks_parent_certificate_before_ack():
    """A leased child must not ACK if persisted ancestry becomes inconsistent."""
    from sose.composition.effects import BusinessEffectApplied
    store = MemoryPersistence()
    bus = BoundaryService(store)
    parent = BoundaryMessage.create(
        contract_name="warehouse.dispatch_ready", contract_version=1,
        source_domain="warehouse_fulfillment", source_identity="order-2",
        destination_domain="logistics", occurrence_key="parent-race",
        correlation_id="flow-race", causation_id=None, produced_at=NOW,
        payload={"shipment_id": "shipment-2"},
    )
    child = BoundaryMessage.create(
        contract_name="logistics.delivery_completed", contract_version=1,
        source_domain="logistics", source_identity="shipment-2",
        destination_domain="order_to_cash", occurrence_key="child-race",
        correlation_id="flow-race", causation_kind="boundary",
        causation_id=parent.message_id, produced_at=NOW + timedelta(seconds=1),
        payload={"order_id": "order-2", "shipment_id": "shipment-2"},
    )
    bus.publish(parent)
    bus.publish(child)
    registry = BoundaryConsumerRegistry()

    def accept(source, uow):
        uow.save_command(Command(
            command_id="effect-race", name="composition.deliver_shipment",
            entity_type="shipment", entity_id="shipment-2", due_at=NOW,
            causation_id=source.message_id, correlation_id=source.correlation_id,
        ))
        return "effect-race"

    registry.register(
        destination_domain="logistics", contract_name="warehouse.dispatch_ready",
        contract_version=1, handler=accept,
    )
    parent_lease = bus.claim_next(
        owner_id="parent-worker", now=NOW, lease_duration=timedelta(hours=1),
        destination_domain="logistics",
    )
    bus.consume(lease=parent_lease, registry=registry, now=NOW)
    with store.transaction() as uow:
        uow.save_entity(Entity(
            id="shipment-2", entity_type="shipment", state="delivered", version=3,
        ))
    proof = BusinessEffectService(store).complete(effect_id="effect-race", completed_at=NOW)
    assert isinstance(proof, BusinessEffectApplied)
    child_lease = bus.claim_next(
        owner_id="child-worker", now=NOW, lease_duration=timedelta(hours=1),
        destination_domain="order_to_cash",
    )
    assert child_lease is not None

    # Model an out-of-band damaged store/import. An ACK cannot rely on the
    # now-stale certificate check performed during claim.
    store._state.business_effects.pop("effect-race")
    child_registry = BoundaryConsumerRegistry()
    child_registry.register(
        destination_domain="order_to_cash",
        contract_name="logistics.delivery_completed", contract_version=1,
        handler=lambda message, uow: "child-effect",
    )
    with pytest.raises(StaleBoundaryClaimError, match="causal predecessor"):
        bus.consume(lease=child_lease, registry=child_registry, now=NOW)
    assert bus.consumption(child_lease.delivery_id) is None

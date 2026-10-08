from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from sose.composition.boundary import (
    BoundaryConsumerRegistry,
    BoundaryMessage,
    BoundaryService,
    DeliveryStatus,
    StaleBoundaryClaimError,
    UnsupportedBoundaryContractError,
)
from sose.domain.entity import Entity
from sose.persistence.memory import MemoryPersistence
from sose.persistence.sqlite import SQLitePersistence


NOW = datetime(2026, 10, 8, 12, 0, 0)


def _message(*, amount: int = 3, occurrence_key: str = "1") -> BoundaryMessage:
    return BoundaryMessage.create(
        contract_name="o2c.fulfillment_requested",
        contract_version=1,
        source_domain="order_to_cash",
        source_identity="order-42",
        destination_domain="warehouse_fulfillment",
        occurrence_key=occurrence_key,
        correlation_id="corr-42",
        causation_id="event-41",
        produced_at=NOW,
        payload={"sku": "A", "qty": amount, "nested": {"b": 2, "a": 1}},
    )


def test_boundary_message_identity_and_payload_are_canonical() -> None:
    left = _message()
    right = BoundaryMessage.create(
        contract_name="o2c.fulfillment_requested",
        contract_version=1,
        source_domain="order_to_cash",
        source_identity="order-42",
        destination_domain="warehouse_fulfillment",
        occurrence_key="1",
        correlation_id="corr-42",
        causation_id="event-41",
        produced_at=NOW,
        payload={"nested": {"a": 1, "b": 2}, "qty": 3, "sku": "A"},
    )

    assert left == right
    assert left.message_id == right.message_id
    assert left.payload_hash == right.payload_hash
    assert left.contract_key == "o2c.fulfillment_requested.v1"
    assert left.payload() == {
        "nested": {"a": 1, "b": 2},
        "qty": 3,
        "sku": "A",
    }


def test_same_semantic_message_with_changed_payload_is_rejected() -> None:
    service = BoundaryService(MemoryPersistence())
    first = _message(amount=3)
    conflicting = _message(amount=4)

    service.publish(first)
    with pytest.raises(ValueError, match="boundary message identity conflict"):
        service.publish(conflicting)


def test_publish_is_idempotent_and_claim_order_is_deterministic() -> None:
    service = BoundaryService(MemoryPersistence())
    later = _message(occurrence_key="later").model_copy(produced_at=NOW + timedelta(seconds=1))
    earlier = _message(occurrence_key="earlier")

    first = service.publish(later)
    assert service.publish(later) == first
    service.publish(earlier)

    lease = service.claim_next(
        owner_id="worker-a",
        now=NOW,
        lease_duration=timedelta(seconds=30),
    )
    assert lease is not None
    assert lease.message_id == earlier.message_id
    assert lease.epoch == 1
    assert service.delivery(lease.delivery_id).status is DeliveryStatus.CLAIMED


def test_expired_claim_is_reclaimed_with_higher_fencing_epoch() -> None:
    service = BoundaryService(MemoryPersistence())
    service.publish(_message())

    first = service.claim_next(
        owner_id="worker-a",
        now=NOW,
        lease_duration=timedelta(seconds=10),
    )
    assert first is not None

    second = service.claim_next(
        owner_id="worker-b",
        now=NOW + timedelta(seconds=11),
        lease_duration=timedelta(seconds=10),
    )
    assert second is not None
    assert second.delivery_id == first.delivery_id
    assert second.epoch == first.epoch + 1
    assert second.owner_id == "worker-b"

    registry = BoundaryConsumerRegistry()
    registry.register(
        destination_domain="warehouse_fulfillment",
        contract_name="o2c.fulfillment_requested",
        contract_version=1,
        handler=lambda message, uow: "effect-1",
    )

    with pytest.raises(StaleBoundaryClaimError):
        service.consume(
            lease=first,
            registry=registry,
            now=NOW + timedelta(seconds=12),
        )


def test_consumer_effect_and_ack_commit_atomically_and_replay_is_idempotent() -> None:
    persistence = MemoryPersistence()
    service = BoundaryService(persistence)
    service.publish(_message())
    lease = service.claim_next(
        owner_id="worker-a",
        now=NOW,
        lease_duration=timedelta(seconds=30),
    )
    assert lease is not None

    calls = 0

    def handler(message, uow):
        nonlocal calls
        calls += 1
        uow.save_entity(
            Entity(
                id="allocation-42",
                entity_type="fulfillment_allocation",
                state="requested",
                attributes={"source_message_id": message.message_id},
            )
        )
        return "allocation-42"

    registry = BoundaryConsumerRegistry()
    registry.register(
        destination_domain="warehouse_fulfillment",
        contract_name="o2c.fulfillment_requested",
        contract_version=1,
        handler=handler,
    )

    consumption = service.consume(lease=lease, registry=registry, now=NOW)
    assert consumption.consumer_effect_id == "allocation-42"
    assert persistence.entity("fulfillment_allocation", "allocation-42") is not None
    assert service.delivery(lease.delivery_id).status is DeliveryStatus.CONSUMED
    assert calls == 1

    replayed = service.consume(lease=lease, registry=registry, now=NOW)
    assert replayed == consumption
    assert calls == 1


def test_failed_consumer_rolls_back_effect_and_ack() -> None:
    persistence = MemoryPersistence()
    service = BoundaryService(persistence)
    service.publish(_message())
    lease = service.claim_next(
        owner_id="worker-a",
        now=NOW,
        lease_duration=timedelta(seconds=30),
    )
    assert lease is not None

    def handler(message, uow):
        uow.save_entity(
            Entity(
                id="allocation-42",
                entity_type="fulfillment_allocation",
                state="requested",
            )
        )
        raise RuntimeError("boom")

    registry = BoundaryConsumerRegistry()
    registry.register(
        destination_domain="warehouse_fulfillment",
        contract_name="o2c.fulfillment_requested",
        contract_version=1,
        handler=handler,
    )

    with pytest.raises(RuntimeError, match="boom"):
        service.consume(lease=lease, registry=registry, now=NOW)

    assert persistence.entity("fulfillment_allocation", "allocation-42") is None
    current = service.delivery(lease.delivery_id)
    assert current.status is DeliveryStatus.CLAIMED
    assert service.consumption(lease.delivery_id) is None


def test_missing_contract_handler_is_rejected_without_ack() -> None:
    service = BoundaryService(MemoryPersistence())
    service.publish(_message())
    lease = service.claim_next(
        owner_id="worker-a",
        now=NOW,
        lease_duration=timedelta(seconds=30),
    )
    assert lease is not None

    with pytest.raises(UnsupportedBoundaryContractError):
        service.consume(
            lease=lease,
            registry=BoundaryConsumerRegistry(),
            now=NOW,
        )
    assert service.delivery(lease.delivery_id).status is DeliveryStatus.CLAIMED


def test_sqlite_restart_preserves_pending_claim_and_idempotent_consumption(tmp_path) -> None:
    path = tmp_path / "composition.sqlite3"

    first = SQLitePersistence(path)
    first_service = BoundaryService(first)
    message = _message()
    first_service.publish(message)
    lease = first_service.claim_next(
        owner_id="worker-a",
        now=NOW,
        lease_duration=timedelta(seconds=5),
    )
    assert lease is not None
    first.close()

    reopened = SQLitePersistence(path)
    service = BoundaryService(reopened)
    reclaimed = service.claim_next(
        owner_id="worker-b",
        now=NOW + timedelta(seconds=6),
        lease_duration=timedelta(seconds=30),
    )
    assert reclaimed is not None
    assert reclaimed.delivery_id == lease.delivery_id
    assert reclaimed.epoch == 2

    registry = BoundaryConsumerRegistry()
    registry.register(
        destination_domain="warehouse_fulfillment",
        contract_name="o2c.fulfillment_requested",
        contract_version=1,
        handler=lambda message, uow: "effect-42",
    )
    consumed = service.consume(
        lease=reclaimed,
        registry=registry,
        now=NOW + timedelta(seconds=7),
    )
    reopened.close()

    final = SQLitePersistence(path)
    final_service = BoundaryService(final)
    assert final_service.delivery(reclaimed.delivery_id).status is DeliveryStatus.CONSUMED
    assert final_service.consumption(reclaimed.delivery_id) == consumed
    final.close()

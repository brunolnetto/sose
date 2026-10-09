"""Adversarial boundary claim/consume protocol validation under durable UoW."""
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from sose.composition.boundary import (
    BoundaryConsumerRegistry, BoundaryService, StaleBoundaryClaimError,
    UnsupportedBoundaryContractError,
)
from sose.composition.model import (
    BoundaryConsumption, BoundaryDelivery, BoundaryLease, BoundaryMessage,
    DeliveryStatus,
)
from sose.persistence.memory import MemoryPersistence


NOW = datetime(2026, 10, 9, 12, tzinfo=timezone.utc)
TTL = timedelta(minutes=5)


def msg(occurrence="one", *, destination="destination", causation=None):
    return BoundaryMessage.create(
        contract_name="test.ready", contract_version=1,
        source_domain="source", source_identity="root",
        destination_domain=destination, occurrence_key=occurrence,
        correlation_id="flow", causation_id=causation,
        produced_at=NOW, payload={"occurrence": occurrence},
    )


def registry(effect="effect"):
    result = BoundaryConsumerRegistry()
    result.register(
        destination_domain="destination", contract_name="test.ready",
        contract_version=1, handler=lambda m, u: effect,
    )
    return result


@pytest.mark.parametrize("fields", [
    {"destination_domain": ""},
    {"contract_name": ""},
    {"contract_version": 0},
])
def test_consumer_registry_rejects_invalid_contracts(fields):
    options = {
        "destination_domain": "destination",
        "contract_name": "test.ready",
        "contract_version": 1,
        "handler": lambda m, u: "effect",
    }
    with pytest.raises(ValueError, match="invalid boundary consumer contract"):
        BoundaryConsumerRegistry().register(**{**options, **fields})


def test_consumer_registry_rejects_duplicate_and_unsupported_versions():
    consumers = registry()
    with pytest.raises(ValueError, match="already registered"):
        consumers.register(
            destination_domain="destination", contract_name="test.ready",
            contract_version=1, handler=lambda m, u: "another",
        )
    with pytest.raises(UnsupportedBoundaryContractError):
        consumers.resolve(replace(msg(), contract_version=2))


def test_publish_is_idempotent_and_rejects_conflicting_identity():
    store = MemoryPersistence()
    service = BoundaryService(store)
    original = msg()
    first = service.publish(original)
    assert service.publish(original) == first
    assert service.delivery(first.delivery_id) == first
    assert service.consumption(first.delivery_id) is None
    with pytest.raises(ValueError, match="boundary message identity conflict"):
        service.publish(replace(original, correlation_id="other-flow"))


def test_publish_rejects_corrupt_existing_delivery():
    store = MemoryPersistence()
    service = BoundaryService(store)
    message = msg()
    first = service.publish(message)
    with store.transaction() as uow:
        uow.save_boundary_delivery(replace(first, destination_domain="corrupted"))
    with pytest.raises(ValueError, match="boundary delivery identity conflict"):
        service.publish(message)


@pytest.mark.parametrize("changes,pattern", [
    ({"owner_id": ""}, "owner_id"),
    ({"destination_domain": ""}, "destination_domain"),
    ({"lease_duration": timedelta(0)}, "lease_duration"),
    ({"accepted_contracts": frozenset({("", 1)})}, "accepted_contracts"),
    ({"accepted_contracts": frozenset({("name", 0)})}, "accepted_contracts"),
    ({"accepted_contracts": frozenset({("name", "v1")})}, "accepted_contracts"),
])
def test_claim_next_rejects_invalid_ownership_or_contract_filters(changes, pattern):
    service = BoundaryService(MemoryPersistence())
    arguments = {"owner_id": "worker", "now": NOW, "lease_duration": TTL}
    with pytest.raises(ValueError, match=pattern):
        service.claim_next(**{**arguments, **changes})


def test_claim_ignores_wrong_destination_and_contract_then_reclaims_expired():
    service = BoundaryService(MemoryPersistence())
    message = msg()
    service.publish(message)
    assert service.claim_next(
        owner_id="wrong", now=NOW, lease_duration=TTL,
        destination_domain="other",
    ) is None
    assert service.claim_next(
        owner_id="wrong", now=NOW, lease_duration=TTL,
        accepted_contracts=frozenset({("other", 1)}),
    ) is None
    first = service.claim_next(
        owner_id="first", now=NOW, lease_duration=TTL,
    )
    assert first is not None
    assert service.claim_next(
        owner_id="second", now=NOW + timedelta(minutes=1),
        lease_duration=TTL,
    ) is None
    second = service.claim_next(
        owner_id="second", now=NOW + timedelta(minutes=6),
        lease_duration=TTL,
    )
    assert second is not None
    assert second.epoch == first.epoch + 1
    with pytest.raises(StaleBoundaryClaimError, match="stale boundary claim"):
        service.consume(lease=first, registry=registry(), now=NOW + timedelta(minutes=6))


def test_claim_fails_closed_when_persisted_delivery_has_no_source_message():
    store = MemoryPersistence()
    service = BoundaryService(store)
    message = msg()
    service.publish(message)
    store._state.boundary_messages.pop(message.message_id)
    with pytest.raises(RuntimeError, match="delivery missing message"):
        service.claim_next(owner_id="worker", now=NOW, lease_duration=TTL)


def test_consume_unknown_delivery_fails_with_identity():
    service = BoundaryService(MemoryPersistence())
    lease = BoundaryLease(
        delivery_id="absent", message_id="absent", owner_id="worker",
        epoch=1, lease_expires_at=NOW + TTL,
    )
    with pytest.raises(KeyError, match="unknown boundary delivery"):
        service.consume(lease=lease, registry=registry(), now=NOW)


def test_consume_expired_claim_fails_closed():
    service = BoundaryService(MemoryPersistence())
    service.publish(msg())
    lease = service.claim_next(owner_id="worker", now=NOW, lease_duration=TTL)
    with pytest.raises(StaleBoundaryClaimError, match="expired boundary claim"):
        service.consume(lease=lease, registry=registry(), now=NOW + TTL)


def test_consume_wrong_owner_fails_closed():
    service = BoundaryService(MemoryPersistence())
    service.publish(msg())
    lease = service.claim_next(owner_id="worker", now=NOW, lease_duration=TTL)
    with pytest.raises(StaleBoundaryClaimError, match="stale boundary claim"):
        service.consume(
            lease=replace(lease, owner_id="intruder"),
            registry=registry(), now=NOW,
        )


def test_consume_empty_handler_effect_is_not_acknowledged():
    service = BoundaryService(MemoryPersistence())
    service.publish(msg())
    lease = service.claim_next(owner_id="worker", now=NOW, lease_duration=TTL)
    with pytest.raises(ValueError, match="effect identity cannot be empty"):
        service.consume(lease=lease, registry=registry(""), now=NOW)
    assert service.consumption(lease.delivery_id) is None


def test_consume_unknown_contract_cannot_acknowledge():
    service = BoundaryService(MemoryPersistence())
    service.publish(msg())
    lease = service.claim_next(owner_id="worker", now=NOW, lease_duration=TTL)
    with pytest.raises(UnsupportedBoundaryContractError):
        service.consume(lease=lease, registry=BoundaryConsumerRegistry(), now=NOW)


def test_consumed_delivery_missing_receipt_is_detected():
    store = MemoryPersistence()
    service = BoundaryService(store)
    published = service.publish(msg())
    with store.transaction() as uow:
        uow.save_boundary_delivery(replace(
            published, status=DeliveryStatus.CONSUMED,
            consumed_at=NOW, consumer_effect_id="effect",
        ))
    lease = BoundaryLease(
        delivery_id=published.delivery_id, message_id=published.message_id,
        owner_id="worker", epoch=1, lease_expires_at=NOW + TTL,
    )
    with pytest.raises(RuntimeError, match="lacks consumption record"):
        service.consume(lease=lease, registry=registry(), now=NOW)


def test_conflicting_existing_consumption_receipt_cannot_be_overwritten():
    store = MemoryPersistence()
    service = BoundaryService(store)
    message = msg()
    service.publish(message)
    lease = service.claim_next(owner_id="worker", now=NOW, lease_duration=TTL)
    with store.transaction() as uow:
        delivery = uow.get_boundary_delivery(lease.delivery_id)
        uow.save_boundary_consumption(BoundaryConsumption.create(
            delivery=delivery, consumer_effect_id="different-effect", consumed_at=NOW,
        ))
    with pytest.raises(ValueError, match="consumption identity conflict"):
        service.consume(lease=lease, registry=registry(), now=NOW)


def test_consumed_delivery_replay_returns_same_receipt():
    service = BoundaryService(MemoryPersistence())
    service.publish(msg())
    lease = service.claim_next(owner_id="worker", now=NOW, lease_duration=TTL)
    first = service.consume(lease=lease, registry=registry(), now=NOW)
    assert service.consume(lease=lease, registry=registry(), now=NOW) == first

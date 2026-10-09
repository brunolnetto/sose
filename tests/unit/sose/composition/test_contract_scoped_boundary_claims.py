"""Destination-specific workers must claim only their registered contracts.

The test exercises memory and SQLite stores, including versioned collisions and
restart; transport ownership must be enforced before entering consumer code.
"""
from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from sose.composition.boundary import (
    BoundaryConsumerRegistry, BoundaryMessage, BoundaryService, DeliveryStatus,
)
from sose.persistence.memory import MemoryPersistence
from sose.persistence.sqlite import SQLitePersistence


NOW = datetime(2026, 10, 9, 11, 0, 0)


def _message(contract_name: str, version: int, occurrence_key: str):
    return BoundaryMessage.create(
        contract_name=contract_name,
        contract_version=version,
        source_domain="order_to_cash",
        source_identity="order-1",
        destination_domain="warehouse_management",
        occurrence_key=occurrence_key,
        correlation_id="order-1",
        causation_id=None,
        produced_at=NOW,
        payload={"key": occurrence_key},
    )


@pytest.mark.parametrize("adapter", ["memory", "sqlite"])
def test_worker_skips_foreign_contract_even_in_same_destination(adapter, tmp_path):
    store = (
        MemoryPersistence() if adapter == "memory"
        else SQLitePersistence(tmp_path / "composition-contract-claims.sqlite")
    )
    service = BoundaryService(store)
    foreign = _message("warehouse.unrelated", 1, "0-foreign")
    previous = _message("warehouse.inventory_consumption_requested", 2, "1-wrong-version")
    owned = _message("warehouse.inventory_consumption_requested", 1, "2-owned")
    for item in (foreign, previous, owned):
        service.publish(item)

    registry = BoundaryConsumerRegistry()
    registry.register(
        destination_domain="warehouse_management",
        contract_name="warehouse.inventory_consumption_requested",
        contract_version=1,
        handler=lambda message, uow: "effect-1",
    )
    accepted = registry.contracts_for("warehouse_management")
    assert accepted == frozenset({("warehouse.inventory_consumption_requested", 1)})
    assert registry.contracts_for("warehouse_fulfillment") == frozenset()

    lease = service.claim_next(
        owner_id="wm-worker", now=NOW, lease_duration=timedelta(minutes=2),
        destination_domain="warehouse_management", accepted_contracts=accepted,
    )
    assert lease is not None and lease.message_id == owned.message_id
    assert service.consume(lease=lease, registry=registry, now=NOW).consumer_effect_id == (
        "effect-1"
    )
    assert service.claim_next(
        owner_id="wm-worker", now=NOW, lease_duration=timedelta(minutes=2),
        destination_domain="warehouse_management", accepted_contracts=accepted,
    ) is None
    for item in (foreign, previous):
        assert service.delivery(service.publish(item).delivery_id).status is DeliveryStatus.PENDING

    if adapter == "sqlite":
        store.close()
        store = SQLitePersistence(tmp_path / "composition-contract-claims.sqlite")
        service = BoundaryService(store)

    assert service.claim_next(
        owner_id="wm-worker", now=NOW, lease_duration=timedelta(minutes=2),
        destination_domain="warehouse_management", accepted_contracts=frozenset(),
    ) is None
    # A different worker owning another handler still gets the other message.
    second = service.claim_next(
        owner_id="foreign-worker", now=NOW, lease_duration=timedelta(minutes=2),
        destination_domain="warehouse_management",
        accepted_contracts=frozenset({("warehouse.unrelated", 1)}),
    )
    assert second is not None and second.message_id == foreign.message_id
    if adapter == "sqlite":
        store.close()


def test_contract_filter_rejects_invalid_version_without_claiming():
    service = BoundaryService(MemoryPersistence())
    msg = _message("warehouse.inventory_consumption_requested", 1, "only")
    service.publish(msg)
    with pytest.raises(ValueError, match="accepted_contracts"):
        service.claim_next(
            owner_id="wm-worker", now=NOW, lease_duration=timedelta(minutes=1),
            destination_domain="warehouse_management",
            accepted_contracts=frozenset({("warehouse.inventory_consumption_requested", 0)}),
        )
    assert service.delivery(service.publish(msg).delivery_id).status is DeliveryStatus.PENDING

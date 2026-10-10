"""One recurring consumer must never lease another domain's boundary work."""
from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from sose.composition.boundary import BoundaryMessage, BoundaryService, DeliveryStatus
from sose.persistence.memory import MemoryPersistence
from sose.persistence.sqlite import SQLitePersistence


NOW = datetime(2026, 10, 9, 12, 0, 0)


def _message(*, destination: str, key: str, when: datetime) -> BoundaryMessage:
    return BoundaryMessage.create(
        contract_name="test.routed",
        contract_version=1,
        source_domain="source",
        source_identity="source-1",
        destination_domain=destination,
        occurrence_key=key,
        correlation_id="correlation-1",
        causation_id=None,
        produced_at=when,
        payload={"source_event": key},
    )


@pytest.mark.parametrize("store_type", ["memory", "sqlite"])
def test_scoped_claim_does_not_steal_other_domain_work(store_type, tmp_path):
    if store_type == "sqlite":
        path = tmp_path / "claims.sqlite"
        persistence = SQLitePersistence(path)
    else:
        persistence = MemoryPersistence()
    service = BoundaryService(persistence)
    foreign = _message(
        destination="warehouse_fulfillment", key="earlier",
        when=NOW,
    )
    owned = _message(
        destination="warehouse_management", key="later",
        when=NOW + timedelta(minutes=1),
    )
    service.publish(foreign)
    service.publish(owned)

    lease = service.claim_next(
        owner_id="wm-worker", now=NOW + timedelta(minutes=1),
        lease_duration=timedelta(minutes=2),
        destination_domain="warehouse_management",
    )
    assert lease is not None
    assert lease.message_id == owned.message_id
    assert service.delivery(lease.delivery_id).status is DeliveryStatus.CLAIMED
    assert service.delivery(
        service.publish(foreign).delivery_id
    ).status is DeliveryStatus.PENDING

    if store_type == "sqlite":
        persistence.close()
        persistence = SQLitePersistence(path)
        service = BoundaryService(persistence)

    lease_wf = service.claim_next(
        owner_id="wf-worker", now=NOW + timedelta(minutes=1),
        lease_duration=timedelta(minutes=2),
        destination_domain="warehouse_fulfillment",
    )
    assert lease_wf is not None and lease_wf.message_id == foreign.message_id
    assert lease_wf.delivery_id != lease.delivery_id

    # Independent destination scope does not bypass fencing of the existing WM claim.
    assert service.claim_next(
        owner_id="wm-concurrent-worker", now=NOW + timedelta(minutes=2),
        lease_duration=timedelta(minutes=2),
        destination_domain="warehouse_management",
    ) is None
    if store_type == "sqlite":
        persistence.close()


def test_destination_filter_rejects_empty_name_without_claiming():
    service = BoundaryService(MemoryPersistence())
    msg = _message(destination="warehouse_fulfillment", key="first", when=NOW)
    service.publish(msg)
    with pytest.raises(ValueError, match="destination_domain"):
        service.claim_next(
            owner_id="worker", now=NOW,
            lease_duration=timedelta(minutes=1),
            destination_domain="",
        )
    lease = service.claim_next(
        owner_id="worker", now=NOW,
        lease_duration=timedelta(minutes=1),
    )
    assert lease is not None and lease.message_id == msg.message_id


@pytest.mark.parametrize("store_type", ["memory", "sqlite"])
def test_exact_message_claim_does_not_steal_another_customer_at_same_destination(
    store_type, tmp_path,
):
    store = (
        MemoryPersistence() if store_type == "memory"
        else SQLitePersistence(tmp_path / "exact-message.sqlite")
    )
    try:
        service = BoundaryService(store)
        other = _message(destination="logistics", key="other-customer", when=NOW)
        owned = _message(
            destination="logistics", key="current-customer",
            when=NOW + timedelta(seconds=1),
        )
        service.publish(other)
        service.publish(owned)

        lease = service.claim_next(
            owner_id="customer-worker", now=NOW + timedelta(seconds=1),
            lease_duration=timedelta(minutes=2),
            destination_domain="logistics",
            message_id=owned.message_id,
        )
        assert lease is not None and lease.message_id == owned.message_id
        assert service.delivery(lease.delivery_id).status is DeliveryStatus.CLAIMED
        assert service.claim_next(
            owner_id="other-worker", now=NOW + timedelta(seconds=1),
            lease_duration=timedelta(minutes=2),
            destination_domain="logistics",
            message_id=other.message_id,
        ).message_id == other.message_id
    finally:
        if store_type == "sqlite":
            store.close()


def test_exact_message_claim_rejects_empty_identity():
    service = BoundaryService(MemoryPersistence())
    with pytest.raises(ValueError, match="message_id"):
        service.claim_next(
            owner_id="worker", now=NOW,
            lease_duration=timedelta(minutes=1), message_id="",
        )

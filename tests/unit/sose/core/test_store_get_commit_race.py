"""Deterministic falsifiers for committed selection appearing during SimPy callback.

Issue #468: separate workers can have a stale ephemeral StoreItem while the
durable store-get result has already been committed by another worker.
"""
from datetime import datetime, timezone

import pytest

from sose.backends.base import StoreItem
from sose.core.runtime import DurableStoreItem, StoreDefinition, StoreGetRequest
from sose.core.stores import DurableStoreManager
from sose.persistence.memory import MemoryPersistence


NOW = datetime(2026, 10, 13, 0, tzinfo=timezone.utc)
BACKEND_ITEM = StoreItem(
    item_id="origin-queue:shipment-1", store_name="origin_hub_queue",
    value={"shipment_id": "shipment-1"},
)


def _seed(*, second_request: bool = False) -> MemoryPersistence:
    store = MemoryPersistence()
    with store.transaction() as uow:
        uow.save_store_definition(StoreDefinition("origin_hub_queue"))
        uow.save_store_item(DurableStoreItem(
            item_id=BACKEND_ITEM.item_id, store_name=BACKEND_ITEM.store_name,
            value=BACKEND_ITEM.value, priority=100, sequence=1,
        ))
        uow.save_store_get_request(StoreGetRequest(
            request_id="origin-dequeue:shipment-1",
            store_name=BACKEND_ITEM.store_name, requested_at=NOW, sequence=1,
        ))
        if second_request:
            uow.save_store_get_request(StoreGetRequest(
                request_id="origin-dequeue:other-shipment",
                store_name=BACKEND_ITEM.store_name, requested_at=NOW, sequence=2,
            ))
    return store


def test_get_returns_existing_exact_request_result_when_competitor_commits_between_reads(
    monkeypatch,
):
    store = _seed()
    manager = DurableStoreManager(store)
    other_worker = DurableStoreManager(store)
    completed = []

    def another_worker_consumes_first(item_id: str):
        assert item_id == BACKEND_ITEM.item_id
        completed.append(other_worker.commit_get(
            "origin-dequeue:shipment-1", BACKEND_ITEM, completed_at=NOW,
        ))
        # The item no longer exists, but the exact selection is durable.
        assert store.store_items() == ()
        return None

    monkeypatch.setattr(manager, "_item_with_id", another_worker_consumes_first)
    actual = manager.commit_get(
        "origin-dequeue:shipment-1", BACKEND_ITEM, completed_at=NOW,
    )

    assert actual == completed[0]
    assert manager.commit_get(
        "origin-dequeue:shipment-1", BACKEND_ITEM, completed_at=NOW,
    ) == actual
    assert store.store_get_requests() == ()
    assert store.store_items() == ()
    assert store.store_get_results() == (actual,)


def test_get_must_not_claim_a_different_request_just_because_item_was_consumed(
    monkeypatch,
):
    store = _seed(second_request=True)
    manager = DurableStoreManager(store)
    other_worker = DurableStoreManager(store)

    def another_worker_consumes_first(_item_id: str):
        other_worker.commit_get(
            "origin-dequeue:shipment-1", BACKEND_ITEM, completed_at=NOW,
        )
        return None

    monkeypatch.setattr(manager, "_item_with_id", another_worker_consumes_first)
    with pytest.raises(KeyError, match="unknown durable store item"):
        manager.commit_get(
            "origin-dequeue:other-shipment", BACKEND_ITEM, completed_at=NOW,
        )

    assert store.store_get_results()[0].request_id == "origin-dequeue:shipment-1"
    assert tuple(r.request_id for r in store.store_get_requests()) == (
        "origin-dequeue:other-shipment",
    )

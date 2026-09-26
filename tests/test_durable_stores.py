from __future__ import annotations

from datetime import datetime, timezone

import pytest

from datetime import timedelta

from sose.backends.simpy import SimPyBackend
from sose.core.clock import SimulationClock
from sose.core.context import SimulationContext
from sose.core.engine import Engine
from sose.core.randomness import RandomSource
from sose.core.scheduler import Scheduler
from sose.domain.registry import DomainRegistry
from sose.core.runtime import (
    DurableStoreItem,
    StoreDefinition,
    StoreGetRequest,
    StorePutIntent,
)
from sose.core.stores import DurableStoreManager
from sose.persistence.memory import MemoryPersistence

NOW = datetime(2026, 1, 1, 8, tzinfo=timezone.utc)


def test_put_is_persisted_before_backend_acceptance_and_committed_after_callback():
    store = MemoryPersistence()
    manager = DurableStoreManager(store)
    backend = SimPyBackend(origin=NOW)
    manager.define(StoreDefinition("inbox", kind="fifo", capacity=1))
    manager.rebuild_backend(backend)

    intent = manager.put(
        backend,
        store_name="inbox",
        item_id="item-1",
        value={"payload": 1},
        requested_at=NOW,
    )

    assert store.store_put_intents() == (intent,)
    assert store.store_items() == ()

    backend.run_until(NOW)

    assert store.store_put_intents() == ()
    assert [item.item_id for item in store.store_items()] == ["item-1"]


def test_get_consumes_item_and_pending_request_atomically():
    store = MemoryPersistence()
    manager = DurableStoreManager(store)
    backend = SimPyBackend(origin=NOW)
    manager.define(StoreDefinition("inbox", kind="fifo"))
    manager.rebuild_backend(backend)

    manager.put(
        backend,
        store_name="inbox",
        item_id="item-1",
        value=1,
        requested_at=NOW,
    )
    backend.run_until(NOW)

    received = []
    request = manager.get(
        backend,
        store_name="inbox",
        request_id="get-1",
        requested_at=NOW,
        on_received=received.append,
    )

    assert store.store_get_requests() == (request,)
    backend.run_until(NOW)

    assert store.store_items() == ()
    assert store.store_get_requests() == ()
    assert received[0].item_id == "item-1"


def test_bounded_store_pending_put_survives_restart_and_completes_after_get():
    store = MemoryPersistence()
    first = DurableStoreManager(store)
    backend1 = SimPyBackend(origin=NOW)
    first.define(StoreDefinition("buffer", kind="fifo", capacity=1))
    first.rebuild_backend(backend1)

    first.put(
        backend1,
        store_name="buffer",
        item_id="first",
        value=1,
        requested_at=NOW,
    )
    backend1.run_until(NOW)
    first.put(
        backend1,
        store_name="buffer",
        item_id="second",
        value=2,
        requested_at=NOW,
    )
    backend1.run_until(NOW)

    assert [i.item_id for i in store.store_items()] == ["first"]
    assert [i.item_id for i in store.store_put_intents()] == ["second"]

    backend2 = SimPyBackend(origin=NOW)
    second = DurableStoreManager(store)
    second.rebuild_backend(backend2)

    received = []
    second.get(
        backend2,
        store_name="buffer",
        request_id="get-1",
        requested_at=NOW,
        on_received=received.append,
    )
    backend2.run_until(NOW)

    assert [i.item_id for i in received] == ["first"]
    assert [i.item_id for i in store.store_items()] == ["second"]
    assert store.store_put_intents() == ()


def test_priority_store_restart_preserves_priority_then_sequence():
    store = MemoryPersistence()
    manager = DurableStoreManager(store)
    backend1 = SimPyBackend(origin=NOW)
    manager.define(StoreDefinition("dispatch", kind="priority"))
    manager.rebuild_backend(backend1)

    manager.put(
        backend1,
        store_name="dispatch",
        item_id="normal-1",
        value=1,
        priority=100,
        requested_at=NOW,
    )
    manager.put(
        backend1,
        store_name="dispatch",
        item_id="urgent",
        value=2,
        priority=1,
        requested_at=NOW,
    )
    manager.put(
        backend1,
        store_name="dispatch",
        item_id="normal-2",
        value=3,
        priority=100,
        requested_at=NOW,
    )
    backend1.run_until(NOW)

    backend2 = SimPyBackend(origin=NOW)
    recovered = DurableStoreManager(store)
    recovered.rebuild_backend(backend2)

    received = []
    for index in range(3):
        recovered.get(
            backend2,
            store_name="dispatch",
            request_id=f"get-{index}",
            requested_at=NOW,
            on_received=received.append,
        )
    backend2.run_until(NOW)

    assert [item.item_id for item in received] == ["urgent", "normal-1", "normal-2"]


def test_filter_store_persists_filter_key_not_callable():
    store = MemoryPersistence()
    filters = {"bearing": lambda item: item.value["kind"] == "bearing"}
    manager = DurableStoreManager(store, filters=filters)
    backend1 = SimPyBackend(origin=NOW)
    manager.define(StoreDefinition("parts", kind="filter"))
    manager.rebuild_backend(backend1)

    manager.put(
        backend1,
        store_name="parts",
        item_id="bolt",
        value={"kind": "bolt"},
        requested_at=NOW,
    )
    backend1.run_until(NOW)

    request = manager.get(
        backend1,
        store_name="parts",
        request_id="bearing-request",
        requested_at=NOW,
        filter_key="bearing",
    )

    assert request.filter_key == "bearing"
    assert store.store_get_requests() == (request,)

    backend2 = SimPyBackend(origin=NOW)
    recovered = DurableStoreManager(store, filters=filters)
    recovered.rebuild_backend(backend2)
    recovered.put(
        backend2,
        store_name="parts",
        item_id="bearing",
        value={"kind": "bearing"},
        requested_at=NOW,
    )
    backend2.run_until(NOW)

    assert store.store_get_requests() == ()
    assert [item.item_id for item in store.store_items()] == ["bolt"]


def test_filter_store_rebuild_requires_registered_filter_key():
    store = MemoryPersistence()
    with store.transaction() as uow:
        uow.save_store_definition(StoreDefinition("parts", kind="filter"))
        uow.save_store_get_request(
            StoreGetRequest(
                request_id="bearing-request",
                store_name="parts",
                requested_at=NOW,
                sequence=1,
                filter_key="bearing",
            )
        )

    with pytest.raises(KeyError, match="unknown durable store filter"):
        DurableStoreManager(store).rebuild_backend(SimPyBackend(origin=NOW))


def test_pending_put_and_get_replay_in_original_operation_order():
    store = MemoryPersistence()
    with store.transaction() as uow:
        uow.save_store_definition(StoreDefinition("buffer", kind="fifo", capacity=1))
        uow.save_store_item(
            DurableStoreItem(
                item_id="holder",
                store_name="buffer",
                value=0,
                priority=100,
                sequence=1,
            )
        )
        uow.save_store_get_request(
            StoreGetRequest(
                request_id="get-holder",
                store_name="buffer",
                requested_at=NOW,
                sequence=2,
            )
        )
        uow.save_store_put_intent(
            StorePutIntent(
                item_id="next",
                store_name="buffer",
                value=1,
                priority=100,
                requested_at=NOW,
                sequence=3,
            )
        )

    backend = SimPyBackend(origin=NOW)
    manager = DurableStoreManager(store)
    manager.rebuild_backend(backend)
    backend.run_until(NOW)

    assert store.store_get_requests() == ()
    assert store.store_put_intents() == ()
    assert [item.item_id for item in store.store_items()] == ["next"]


def test_store_definition_and_ids_are_validated():
    with pytest.raises(ValueError, match="kind"):
        StoreDefinition("bad", kind="stack")
    with pytest.raises(ValueError, match="capacity"):
        StoreDefinition("bad", kind="fifo", capacity=0)

    store = MemoryPersistence()
    manager = DurableStoreManager(store)
    manager.define(StoreDefinition("inbox", kind="fifo"))
    backend = SimPyBackend(origin=NOW)
    manager.rebuild_backend(backend)
    manager.put(
        backend,
        store_name="inbox",
        item_id="same",
        value=1,
        requested_at=NOW,
    )

    with pytest.raises(ValueError, match="already exists"):
        manager.put(
            backend,
            store_name="inbox",
            item_id="same",
            value=2,
            requested_at=NOW,
        )


def _engine(store, *, filters=None):
    context = SimulationContext(
        clock=SimulationClock(now=NOW, step=timedelta(hours=1)),
        random=RandomSource(42),
        scheduler=Scheduler(),
    )
    return Engine(
        context=context,
        registry=DomainRegistry(),
        persistence=store,
        store_filters=filters,
    )


def test_engine_rebuild_restores_durable_store_content():
    store = MemoryPersistence()
    with store.transaction() as uow:
        uow.save_store_definition(StoreDefinition("inbox", kind="fifo"))
        uow.save_store_item(
            DurableStoreItem(
                item_id="persisted",
                store_name="inbox",
                value={"value": 1},
                priority=100,
                sequence=1,
            )
        )

    engine = _engine(store)
    backend = SimPyBackend(origin=NOW)
    engine.rebuild_backend(backend)

    received = []
    engine.stores.get(
        backend,
        store_name="inbox",
        request_id="get-persisted",
        requested_at=NOW,
        on_received=received.append,
    )
    backend.run_until(NOW)

    assert [item.item_id for item in received] == ["persisted"]
    assert store.store_items() == ()


def test_invalid_store_recovery_is_rejected_before_resources_are_rebuilt():
    from sose.core.runtime import ResourceDefinition, ResourceDemand

    store = MemoryPersistence()
    with store.transaction() as uow:
        uow.save_resource_definition(ResourceDefinition("bay", capacity=1))
        uow.save_resource_demand(ResourceDemand("waiter", "bay", 10, NOW, 1))
        uow.save_store_definition(StoreDefinition("parts", kind="filter"))
        uow.save_store_get_request(
            StoreGetRequest(
                request_id="bearing-request",
                store_name="parts",
                requested_at=NOW,
                sequence=2,
                filter_key="missing-filter",
            )
        )

    engine = _engine(store)
    backend = SimPyBackend(origin=NOW)

    with pytest.raises(KeyError, match="unknown durable store filter"):
        engine.rebuild_backend(backend)

    with pytest.raises(KeyError, match="unknown resource"):
        backend.resource_snapshot("bay")
    assert [d.request_id for d in store.resource_demands()] == ["waiter"]

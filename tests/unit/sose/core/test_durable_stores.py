from __future__ import annotations

from datetime import datetime, timezone

import pytest

from datetime import timedelta

from sose.backends.base import StoreItem
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
    StoreGetResult,
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


def test_replayed_get_persists_completion_result_without_live_callback():
    store = MemoryPersistence()
    first = DurableStoreManager(store)
    backend1 = SimPyBackend(origin=NOW)
    first.define(StoreDefinition("inbox", kind="fifo"))
    first.rebuild_backend(backend1)

    request = first.get(
        backend1,
        store_name="inbox",
        request_id="replayed-get",
        requested_at=NOW,
    )
    assert store.store_get_requests() == (request,)
    assert store.store_get_results() == ()

    backend2 = SimPyBackend(origin=NOW)
    recovered = DurableStoreManager(store)
    recovered.rebuild_backend(backend2)
    recovered.put(
        backend2,
        store_name="inbox",
        item_id="after-restart",
        value={"payload": 42},
        requested_at=NOW,
    )
    backend2.run_until(NOW)

    assert store.store_get_requests() == ()
    assert store.store_items() == ()
    results = store.store_get_results()
    assert len(results) == 1
    assert results[0].request_id == "replayed-get"
    assert results[0].item.item_id == "after-restart"
    assert results[0].item.value == {"payload": 42}
    assert recovered.result("replayed-get") == results[0]


def test_completed_get_request_id_cannot_be_reused_or_leave_pending_state():
    store = MemoryPersistence()
    manager = DurableStoreManager(store)
    backend = SimPyBackend(origin=NOW)
    manager.define(StoreDefinition("inbox", kind="fifo"))
    manager.rebuild_backend(backend)

    manager.put(
        backend,
        store_name="inbox",
        item_id="first",
        value=1,
        requested_at=NOW,
    )
    backend.run_until(NOW)
    manager.get(
        backend,
        store_name="inbox",
        request_id="stable-get-id",
        requested_at=NOW,
    )
    backend.run_until(NOW)

    assert [r.request_id for r in store.store_get_results()] == ["stable-get-id"]
    assert store.store_get_requests() == ()

    with pytest.raises(ValueError, match="already exists"):
        manager.get(
            backend,
            store_name="inbox",
            request_id="stable-get-id",
            requested_at=NOW,
        )

    assert store.store_get_requests() == ()
    assert [r.request_id for r in store.store_get_results()] == ["stable-get-id"]


def test_store_get_result_is_terminal_identity_in_persistence():
    store = MemoryPersistence()
    request = StoreGetRequest(
        request_id="terminal",
        store_name="inbox",
        requested_at=NOW,
        sequence=1,
    )
    item = DurableStoreItem(
        item_id="consumed",
        store_name="inbox",
        value=1,
        priority=100,
        sequence=2,
    )
    result = StoreGetResult(
        request_id="terminal",
        store_name="inbox",
        item=item,
        completed_at=NOW,
        sequence=1,
    )

    with store.transaction() as uow:
        uow.save_store_definition(StoreDefinition("inbox"))
        uow.save_store_get_request(request)
        uow.save_store_get_result(result)
        uow.delete_store_get_request("terminal")

    with pytest.raises(ValueError, match="already completed"):
        with store.transaction() as uow:
            uow.save_store_get_request(request)

    assert store.store_get_requests() == ()
    assert store.store_get_results() == (result,)


def test_ensure_selection_returns_committed_fifo_result_after_item_is_consumed():
    store = MemoryPersistence()
    manager = DurableStoreManager(store)
    backend = SimPyBackend(origin=NOW)
    manager.define(StoreDefinition("inbox", kind="fifo"))
    manager.rebuild_backend(backend)

    manager.put(
        backend,
        store_name="inbox",
        item_id="item-1",
        value={"payload": 1},
        requested_at=NOW,
    )
    backend.run_until(NOW)

    first = manager.ensure_selection(
        backend,
        store_name="inbox",
        request_id="pick-1",
        requested_at=NOW,
    )
    second = manager.ensure_selection(
        backend,
        store_name="inbox",
        request_id="pick-1",
        requested_at=NOW,
    )

    assert first is not None
    assert second == first
    assert first.item.item_id == "item-1"
    assert store.store_items() == ()
    assert store.store_get_requests() == ()
    assert manager.selection("pick-1") == first


def test_ensure_selection_preserves_priority_store_semantics():
    store = MemoryPersistence()
    manager = DurableStoreManager(store)
    backend = SimPyBackend(origin=NOW)
    manager.define(StoreDefinition("dispatch", kind="priority"))
    manager.rebuild_backend(backend)

    manager.put(
        backend,
        store_name="dispatch",
        item_id="normal",
        value="normal",
        priority=100,
        requested_at=NOW,
    )
    manager.put(
        backend,
        store_name="dispatch",
        item_id="urgent",
        value="urgent",
        priority=1,
        requested_at=NOW,
    )
    backend.run_until(NOW)

    result = manager.ensure_selection(
        backend,
        store_name="dispatch",
        request_id="dispatch-1",
        requested_at=NOW,
    )

    assert result is not None
    assert result.item.item_id == "urgent"
    assert [item.item_id for item in store.store_items()] == ["normal"]


def test_ensure_selection_preserves_filter_store_semantics_across_restart():
    store = MemoryPersistence()
    filters = {"bearing": lambda item: item.value["kind"] == "bearing"}
    first = DurableStoreManager(store, filters=filters)
    backend1 = SimPyBackend(origin=NOW)
    first.define(StoreDefinition("parts", kind="filter"))
    first.rebuild_backend(backend1)

    pending = first.ensure_selection(
        backend1,
        store_name="parts",
        request_id="bearing-pick",
        requested_at=NOW,
        filter_key="bearing",
    )
    assert pending is None
    assert first.pending_get("bearing-pick") is not None

    backend2 = SimPyBackend(origin=NOW)
    recovered = DurableStoreManager(store, filters=filters)
    recovered.rebuild_backend(backend2)
    recovered.put(
        backend2,
        store_name="parts",
        item_id="bolt",
        value={"kind": "bolt"},
        requested_at=NOW,
    )
    recovered.put(
        backend2,
        store_name="parts",
        item_id="bearing",
        value={"kind": "bearing"},
        requested_at=NOW,
    )
    backend2.run_until(NOW)

    result = recovered.ensure_selection(
        backend2,
        store_name="parts",
        request_id="bearing-pick",
        requested_at=NOW,
        filter_key="bearing",
    )
    assert result is not None
    assert result.item.item_id == "bearing"
    assert [item.item_id for item in store.store_items()] == ["bolt"]


def test_ensure_selection_rejects_request_identity_reuse_for_other_store():
    store = MemoryPersistence()
    manager = DurableStoreManager(store)
    backend = SimPyBackend(origin=NOW)
    manager.define(StoreDefinition("one", kind="fifo"))
    manager.define(StoreDefinition("two", kind="fifo"))
    manager.rebuild_backend(backend)

    manager.put(
        backend,
        store_name="one",
        item_id="item",
        value=1,
        requested_at=NOW,
    )
    backend.run_until(NOW)
    assert manager.ensure_selection(
        backend,
        store_name="one",
        request_id="pick",
        requested_at=NOW,
    ) is not None

    with pytest.raises(RuntimeError, match="belongs to one"):
        manager.ensure_selection(
            backend,
            store_name="two",
            request_id="pick",
            requested_at=NOW,
        )


def test_ensure_put_returns_existing_and_consumed_items_without_new_backend_work():
    store = MemoryPersistence()
    manager = DurableStoreManager(store)
    with store.transaction() as uow:
        uow.save_store_definition(StoreDefinition("inbox", kind="fifo"))
        existing = DurableStoreItem(
            item_id="existing-item",
            store_name="inbox",
            value={"payload": 1},
            priority=100,
            sequence=1,
        )
        uow.save_store_item(existing)
        consumed_item = DurableStoreItem(
            item_id="consumed-item",
            store_name="inbox",
            value={"payload": 2},
            priority=100,
            sequence=2,
        )
        uow.save_store_get_request(
            StoreGetRequest(
                request_id="completed-get",
                store_name="inbox",
                requested_at=NOW,
                sequence=3,
            )
        )
        uow.save_store_get_result(
            StoreGetResult(
                request_id="completed-get",
                store_name="inbox",
                item=consumed_item,
                completed_at=NOW,
                sequence=3,
            )
        )
        uow.delete_store_get_request("completed-get")

    assert manager.ensure_put(
        object(),
        store_name="inbox",
        item_id="existing-item",
        value={"payload": 1},
        requested_at=NOW,
    ) == existing
    assert manager.ensure_put(
        object(),
        store_name="inbox",
        item_id="consumed-item",
        value={"payload": 2},
        requested_at=NOW,
    ) == consumed_item

    with pytest.raises(ValueError, match="identity conflict"):
        manager.ensure_put(
            object(),
            store_name="inbox",
            item_id="existing-item",
            value={"payload": 99},
            requested_at=NOW,
        )
    with pytest.raises(ValueError, match="identity conflict"):
        manager.ensure_put(
            object(),
            store_name="inbox",
            item_id="consumed-item",
            value={"payload": 99},
            requested_at=NOW,
        )


def test_ensure_put_handles_pending_identity_and_run_until_path():
    store = MemoryPersistence()
    manager = DurableStoreManager(store)
    backend = SimPyBackend(origin=NOW)
    manager.define(StoreDefinition("inbox", kind="fifo"))
    manager.rebuild_backend(backend)

    pending = StorePutIntent(
        item_id="pending-item",
        store_name="inbox",
        value={"payload": 1},
        priority=100,
        requested_at=NOW,
        sequence=1,
    )
    with store.transaction() as uow:
        uow.save_store_put_intent(pending)

    assert manager.ensure_put(
        backend,
        store_name="inbox",
        item_id="pending-item",
        value={"payload": 1},
        requested_at=NOW,
    ) is None

    with pytest.raises(ValueError, match="identity conflict"):
        manager.ensure_put(
            backend,
            store_name="inbox",
            item_id="pending-item",
            value={"payload": 1},
            priority=101,
            requested_at=NOW,
        )

    created = manager.ensure_put(
        backend,
        store_name="inbox",
        item_id="new-item",
        value={"payload": 2},
        requested_at=NOW,
    )
    assert created is not None
    assert created.item_id == "new-item"


def test_commit_get_rejects_invalid_inputs_and_missing_durable_state():
    store = MemoryPersistence()
    manager = DurableStoreManager(store)
    with store.transaction() as uow:
        uow.save_store_definition(StoreDefinition("inbox", kind="fifo"))
        uow.save_store_get_request(
            StoreGetRequest(
                request_id="pick",
                store_name="inbox",
                requested_at=NOW,
                sequence=1,
            )
        )

    with pytest.raises(KeyError, match="unknown store get request"):
        manager.commit_get(
            "missing",
            StoreItem("item-1", "inbox", 1),
            completed_at=NOW,
        )
    with pytest.raises(RuntimeError, match="wrong store"):
        manager.commit_get(
            "pick",
            StoreItem("item-1", "other", 1),
            completed_at=NOW,
        )
    with pytest.raises(KeyError, match="unknown durable store item"):
        manager.commit_get(
            "pick",
            StoreItem("item-1", "inbox", 1),
            completed_at=NOW,
        )


def test_commit_get_supports_consuming_item_from_pending_put_intent():
    store = MemoryPersistence()
    manager = DurableStoreManager(store)
    with store.transaction() as uow:
        uow.save_store_definition(StoreDefinition("inbox", kind="fifo"))
        uow.save_store_get_request(
            StoreGetRequest(
                request_id="pick",
                store_name="inbox",
                requested_at=NOW,
                sequence=2,
            )
        )
        uow.save_store_put_intent(
            StorePutIntent(
                item_id="from-intent",
                store_name="inbox",
                value={"payload": 3},
                priority=100,
                requested_at=NOW,
                sequence=1,
            )
        )

    result = manager.commit_get(
        "pick",
        StoreItem("from-intent", "inbox", {"payload": 3}),
        completed_at=NOW,
    )

    assert result.item.item_id == "from-intent"
    assert store.store_put_intents() == ()
    assert store.store_get_requests() == ()
    assert store.store_get_results() == (result,)


def test_commit_get_detects_changed_request_race_paths():
    request = StoreGetRequest(
        request_id="pick",
        store_name="inbox",
        requested_at=NOW,
        sequence=1,
    )
    durable_item = DurableStoreItem(
        item_id="item-1",
        store_name="inbox",
        value={"payload": 1},
        priority=100,
        sequence=2,
    )
    completed = StoreGetResult(
        request_id="pick",
        store_name="inbox",
        item=durable_item,
        completed_at=NOW,
        sequence=1,
    )

    class _RaceUow:
        def __init__(self, *, existing):
            self._existing = existing

        def get_store_get_request(self, request_id):
            return None

        def get_store_get_result(self, request_id):
            return self._existing

        def get_store_item(self, item_id):
            return durable_item

        def get_store_put_intent(self, item_id):
            return None

        def delete_store_item(self, item_id):
            raise AssertionError("race branch should return/raise before mutation")

        def delete_store_put_intent(self, item_id):
            raise AssertionError("race branch should return/raise before mutation")

        def save_store_get_result(self, result):
            raise AssertionError("race branch should return/raise before mutation")

        def delete_store_get_request(self, request_id):
            raise AssertionError("race branch should return/raise before mutation")

    class _RaceTransaction:
        def __init__(self, uow):
            self._uow = uow

        def __enter__(self):
            return self._uow

        def __exit__(self, exc_type, exc, tb):
            return False

    class _RacePersistence:
        def __init__(self, existing):
            self._existing = existing

        def store_get_results(self):
            return ()

        def store_get_requests(self):
            return (request,)

        def store_items(self):
            return (durable_item,)

        def store_put_intents(self):
            return ()

        def transaction(self):
            return _RaceTransaction(_RaceUow(existing=self._existing))

    manager = DurableStoreManager(_RacePersistence(existing=completed))
    assert manager.commit_get(
        "pick",
        StoreItem("item-1", "inbox", {"payload": 1}),
        completed_at=NOW,
    ) == completed

    manager = DurableStoreManager(_RacePersistence(existing=None))
    with pytest.raises(RuntimeError, match="store get request changed"):
        manager.commit_get(
            "pick",
            StoreItem("item-1", "inbox", {"payload": 1}),
            completed_at=NOW,
        )


def test_validate_rebuild_rejects_unknown_store_references():
    class _InvalidPersistence:
        def store_definitions(self):
            return ()

        def store_items(self):
            return (
                DurableStoreItem(
                    item_id="item-1",
                    store_name="ghost",
                    value=1,
                    priority=100,
                    sequence=1,
                ),
            )

        def store_put_intents(self):
            return ()

        def store_get_requests(self):
            return ()

        def store_get_results(self):
            return ()

    manager = DurableStoreManager(_InvalidPersistence())
    with pytest.raises(RuntimeError, match="item references unknown definition"):
        manager.validate_rebuild()

    class _InvalidIntentPersistence:
        def store_definitions(self):
            return ()

        def store_items(self):
            return ()

        def store_put_intents(self):
            return (
                StorePutIntent(
                    item_id="intent-1",
                    store_name="ghost",
                    value=1,
                    priority=100,
                    requested_at=NOW,
                    sequence=1,
                ),
            )

        def store_get_requests(self):
            return ()

        def store_get_results(self):
            return ()

    manager = DurableStoreManager(_InvalidIntentPersistence())
    with pytest.raises(RuntimeError, match="put references unknown definition"):
        manager.validate_rebuild()

    class _InvalidRequestPersistence:
        def store_definitions(self):
            return ()

        def store_items(self):
            return ()

        def store_put_intents(self):
            return ()

        def store_get_requests(self):
            return (
                StoreGetRequest(
                    request_id="get-1",
                    store_name="ghost",
                    requested_at=NOW,
                    sequence=1,
                ),
            )

        def store_get_results(self):
            return ()

    manager = DurableStoreManager(_InvalidRequestPersistence())
    with pytest.raises(RuntimeError, match="get references unknown definition"):
        manager.validate_rebuild()


def test_validate_rebuild_rejects_invalid_filter_and_capacity_overflow():
    store = MemoryPersistence()
    manager = DurableStoreManager(store)
    with store.transaction() as uow:
        uow.save_store_definition(StoreDefinition("inbox", kind="fifo"))
        uow.save_store_get_request(
            StoreGetRequest(
                request_id="get-filtered",
                store_name="inbox",
                requested_at=NOW,
                sequence=1,
                filter_key="only-priority",
            )
        )
    with pytest.raises(RuntimeError, match="non-filter store"):
        manager.validate_rebuild()

    store = MemoryPersistence()
    manager = DurableStoreManager(store)
    with store.transaction() as uow:
        uow.save_store_definition(StoreDefinition("inbox", kind="fifo", capacity=1))
        uow.save_store_item(
            DurableStoreItem(
                item_id="item-1",
                store_name="inbox",
                value=1,
                priority=100,
                sequence=1,
            )
        )
        uow.save_store_item(
            DurableStoreItem(
                item_id="item-2",
                store_name="inbox",
                value=2,
                priority=100,
                sequence=2,
            )
        )
    with pytest.raises(RuntimeError, match="more items than capacity"):
        manager.validate_rebuild()


def test_get_rejects_filter_key_for_non_filter_store():
    store = MemoryPersistence()
    manager = DurableStoreManager(store)
    manager.define(StoreDefinition("inbox", kind="fifo"))

    with pytest.raises(ValueError, match="supported only for filter stores"):
        manager.get(
            SimPyBackend(origin=NOW),
            store_name="inbox",
            request_id="pick",
            requested_at=NOW,
            filter_key="any",
        )


def test_commit_put_returns_existing_and_none_paths():
    store = MemoryPersistence()
    manager = DurableStoreManager(store)
    with store.transaction() as uow:
        uow.save_store_definition(StoreDefinition("inbox", kind="fifo"))
        existing = DurableStoreItem(
            item_id="existing",
            store_name="inbox",
            value=1,
            priority=100,
            sequence=1,
        )
        uow.save_store_item(existing)

    assert manager.commit_put("existing") == existing
    assert manager.commit_put("missing") is None


def test_commit_get_returns_existing_result_without_revalidating_item():
    store = MemoryPersistence()
    manager = DurableStoreManager(store)
    durable_item = DurableStoreItem(
        item_id="item-1",
        store_name="inbox",
        value=1,
        priority=100,
        sequence=1,
    )
    with store.transaction() as uow:
        uow.save_store_definition(StoreDefinition("inbox", kind="fifo"))
        uow.save_store_get_request(
            StoreGetRequest(
                request_id="pick",
                store_name="inbox",
                requested_at=NOW,
                sequence=2,
            )
        )
        uow.save_store_item(durable_item)
        uow.save_store_get_result(
            StoreGetResult(
                request_id="pick",
                store_name="inbox",
                item=durable_item,
                completed_at=NOW,
                sequence=2,
            )
        )
        uow.delete_store_get_request("pick")

    result = manager.commit_get("pick", StoreItem("item-1", "inbox", 1), completed_at=NOW)
    assert result.request_id == "pick"


def test_commit_get_detects_disappeared_item_during_transaction():
    request = StoreGetRequest(
        request_id="pick",
        store_name="inbox",
        requested_at=NOW,
        sequence=1,
    )
    durable_item = DurableStoreItem(
        item_id="item-1",
        store_name="inbox",
        value={"payload": 1},
        priority=100,
        sequence=2,
    )

    class _RaceUow:
        def get_store_get_request(self, request_id):
            return request

        def get_store_get_result(self, request_id):
            return None

        def get_store_item(self, item_id):
            return None

        def get_store_put_intent(self, item_id):
            return None

        def delete_store_item(self, item_id):
            raise AssertionError("should fail before mutation")

        def delete_store_put_intent(self, item_id):
            raise AssertionError("should fail before mutation")

        def save_store_get_result(self, result):
            raise AssertionError("should fail before mutation")

        def delete_store_get_request(self, request_id):
            raise AssertionError("should fail before mutation")

    class _RaceTransaction:
        def __enter__(self):
            return _RaceUow()

        def __exit__(self, exc_type, exc, tb):
            return False

    class _RacePersistence:
        def store_get_results(self):
            return ()

        def store_get_requests(self):
            return (request,)

        def store_items(self):
            return (durable_item,)

        def store_put_intents(self):
            return ()

        def transaction(self):
            return _RaceTransaction()

    manager = DurableStoreManager(_RacePersistence())
    with pytest.raises(RuntimeError, match="disappeared during consume"):
        manager.commit_get(
            "pick",
            StoreItem("item-1", "inbox", {"payload": 1}),
            completed_at=NOW,
        )


def test_ensure_selection_rejects_pending_request_store_filter_mismatch():
    store = MemoryPersistence()
    manager = DurableStoreManager(store, filters={"allowed": lambda _: True})
    backend = SimPyBackend(origin=NOW)
    manager.define(StoreDefinition("orders", kind="filter"))
    manager.rebuild_backend(backend)
    with store.transaction() as uow:
        uow.save_store_get_request(
            StoreGetRequest(
                request_id="pick",
                store_name="orders",
                requested_at=NOW,
                sequence=1,
                filter_key="allowed",
            )
        )

    with pytest.raises(RuntimeError, match="does not match requested store/filter"):
        manager.ensure_selection(
            backend,
            store_name="orders",
            request_id="pick",
            requested_at=NOW,
            filter_key=None,
        )


def test_unknown_store_definition_errors_are_explicit():
    manager = DurableStoreManager(MemoryPersistence())

    with pytest.raises(KeyError, match="unknown store definition"):
        manager.get(
            SimPyBackend(origin=NOW),
            store_name="missing",
            request_id="pick",
            requested_at=NOW,
        )

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from sose.backends.base import StoreItem
from sose.backends.simpy import SimPyBackend
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


class _SnapshotPersistence:
    def __init__(
        self,
        *,
        definitions=(),
        items=(),
        intents=(),
        requests=(),
    ):
        self._definitions = tuple(definitions)
        self._items = tuple(items)
        self._intents = tuple(intents)
        self._requests = tuple(requests)

    def store_definitions(self):
        return self._definitions

    def store_items(self):
        return self._items

    def store_put_intents(self):
        return self._intents

    def store_get_requests(self):
        return self._requests


def _item(item_id: str, store_name: str, *, sequence: int = 1, value=1):
    return DurableStoreItem(item_id, store_name, value, 100, sequence)


def _intent(item_id: str, store_name: str, *, sequence: int = 1, value=1):
    return StorePutIntent(item_id, store_name, value, 100, NOW, sequence)


def _request(
    request_id: str,
    store_name: str,
    *,
    sequence: int = 1,
    filter_key: str | None = None,
):
    return StoreGetRequest(request_id, store_name, NOW, sequence, filter_key)


@pytest.mark.parametrize(
    ("persistence", "message"),
    [
        (
            _SnapshotPersistence(items=(_item("orphan", "missing"),)),
            "store item references unknown definition",
        ),
        (
            _SnapshotPersistence(intents=(_intent("orphan", "missing"),)),
            "store put references unknown definition",
        ),
        (
            _SnapshotPersistence(requests=(_request("orphan", "missing"),)),
            "store get references unknown definition",
        ),
        (
            _SnapshotPersistence(
                definitions=(StoreDefinition("fifo"),),
                requests=(_request("filtered", "fifo", filter_key="wanted"),),
            ),
            "filtered get targets non-filter store",
        ),
        (
            _SnapshotPersistence(
                definitions=(StoreDefinition("bounded", capacity=1),),
                items=(
                    _item("one", "bounded", sequence=1),
                    _item("two", "bounded", sequence=2),
                ),
            ),
            "contains more items than capacity",
        ),
    ],
)
def test_store_rebuild_rejects_corrupt_durable_truth(persistence, message):
    with pytest.raises(RuntimeError, match=message):
        DurableStoreManager(persistence).validate_rebuild()


def _runtime(*, kind="fifo", capacity=None, filters=None):
    persistence = MemoryPersistence()
    manager = DurableStoreManager(persistence, filters=filters)
    backend = SimPyBackend(origin=NOW)
    manager.define(StoreDefinition("store", kind=kind, capacity=capacity))
    manager.rebuild_backend(backend)
    return persistence, manager, backend


def test_ensure_put_distinguishes_existing_consumed_and_pending_identity_conflicts():
    persistence, manager, backend = _runtime(capacity=1)

    manager.put(
        backend,
        store_name="store",
        item_id="holder",
        value={"v": 1},
        requested_at=NOW,
    )
    backend.run_until(NOW)

    existing = manager.ensure_put(
        backend,
        store_name="store",
        item_id="holder",
        value={"v": 1},
        requested_at=NOW,
    )
    assert existing is not None and existing.item_id == "holder"

    with pytest.raises(ValueError, match="identity conflict"):
        manager.ensure_put(
            backend,
            store_name="store",
            item_id="holder",
            value={"v": 2},
            requested_at=NOW,
        )

    manager.get(
        backend,
        store_name="store",
        request_id="consume-holder",
        requested_at=NOW,
        on_received=lambda _: None,
    )
    backend.run_until(NOW)

    consumed = manager.ensure_put(
        backend,
        store_name="store",
        item_id="holder",
        value={"v": 1},
        requested_at=NOW,
    )
    assert consumed is not None and consumed.item_id == "holder"

    with pytest.raises(ValueError, match="identity conflict"):
        manager.ensure_put(
            backend,
            store_name="store",
            item_id="holder",
            value={"v": 3},
            requested_at=NOW,
        )

    manager.put(
        backend,
        store_name="store",
        item_id="blocker",
        value=0,
        requested_at=NOW,
    )
    backend.run_until(NOW)
    manager.put(
        backend,
        store_name="store",
        item_id="pending",
        value=9,
        requested_at=NOW,
        priority=7,
    )

    assert manager.ensure_put(
        backend,
        store_name="store",
        item_id="pending",
        value=9,
        priority=7,
        requested_at=NOW,
    ) is None

    with pytest.raises(ValueError, match="identity conflict"):
        manager.ensure_put(
            backend,
            store_name="store",
            item_id="pending",
            value=9,
            priority=8,
            requested_at=NOW,
        )


def test_store_get_rejects_unknown_store_and_filter_on_non_filter_store():
    _, manager, backend = _runtime()

    with pytest.raises(KeyError, match="unknown store definition"):
        manager.get(
            backend,
            store_name="missing",
            request_id="unknown",
            requested_at=NOW,
        )

    with pytest.raises(ValueError, match="filter_key is supported only for filter stores"):
        manager.get(
            backend,
            store_name="store",
            request_id="filtered",
            requested_at=NOW,
            filter_key="wanted",
        )


def test_commit_put_is_idempotent_for_committed_or_missing_intent():
    persistence, manager, backend = _runtime()
    observed = []

    manager.put(
        backend,
        store_name="store",
        item_id="accepted",
        value=1,
        requested_at=NOW,
        on_stored=observed.append,
    )
    backend.run_until(NOW)

    existing = manager.commit_put("accepted")
    assert existing is not None and existing.item_id == "accepted"
    assert [item.item_id for item in observed] == ["accepted"]
    assert manager.commit_put("never-submitted") is None
    assert persistence.store_put_intents() == ()


class _CommitPutRacePersistence:
    def __init__(self, intent, persisted):
        self.intent = intent
        self.persisted = persisted

    def store_items(self):
        return ()

    def store_put_intents(self):
        return (self.intent,)

    @contextmanager
    def transaction(self):
        yield SimpleNamespace(get_store_put_intent=lambda item_id: self.persisted)


def test_commit_put_rejects_intent_changed_inside_transaction():
    intent = _intent("racy", "store")
    changed = _intent("racy", "store", value=2)
    manager = DurableStoreManager(_CommitPutRacePersistence(intent, changed))

    with pytest.raises(RuntimeError, match="store put intent changed before commit"):
        manager.commit_put("racy")


def test_commit_get_returns_existing_result_and_rejects_missing_wrong_or_unknown_inputs():
    persistence, manager, backend = _runtime()
    manager.put(
        backend,
        store_name="store",
        item_id="item",
        value=1,
        requested_at=NOW,
    )
    backend.run_until(NOW)
    manager.get(
        backend,
        store_name="store",
        request_id="done",
        requested_at=NOW,
    )
    backend.run_until(NOW)
    result = manager.selection("done")
    assert result is not None

    assert manager.commit_get(
        "done",
        StoreItem("irrelevant", "anywhere", object()),
        completed_at=NOW,
    ) == result

    with pytest.raises(KeyError, match="unknown store get request"):
        manager.commit_get(
            "missing",
            StoreItem("item", "store", 1),
            completed_at=NOW,
        )

    manager.get(
        backend,
        store_name="store",
        request_id="pending",
        requested_at=NOW,
    )
    with pytest.raises(RuntimeError, match="wrong store"):
        manager.commit_get(
            "pending",
            StoreItem("foreign", "other", 1),
            completed_at=NOW,
        )

    with pytest.raises(KeyError, match="unknown durable store item"):
        manager.commit_get(
            "pending",
            StoreItem("unknown", "store", 1),
            completed_at=NOW,
        )


class _CommitGetRaceUow:
    def __init__(
        self,
        *,
        request,
        item=None,
        intent=None,
        result=None,
    ):
        self.request = request
        self.item = item
        self.intent = intent
        self.result = result
        self.deleted_items = []
        self.deleted_intents = []
        self.saved_results = []
        self.deleted_requests = []

    def get_store_get_request(self, request_id):
        return self.request

    def get_store_get_result(self, request_id):
        return self.result

    def get_store_item(self, item_id):
        return self.item

    def get_store_put_intent(self, item_id):
        return self.intent

    def delete_store_item(self, item_id):
        self.deleted_items.append(item_id)

    def delete_store_put_intent(self, item_id):
        self.deleted_intents.append(item_id)

    def save_store_get_result(self, result):
        self.saved_results.append(result)

    def delete_store_get_request(self, request_id):
        self.deleted_requests.append(request_id)


class _CommitGetRacePersistence:
    def __init__(
        self,
        *,
        request,
        item=None,
        intent=None,
        transaction_request=None,
        transaction_item=None,
        transaction_intent=None,
        transaction_result=None,
    ):
        self.request = request
        self.item = item
        self.intent = intent
        self.uow = _CommitGetRaceUow(
            request=transaction_request,
            item=transaction_item,
            intent=transaction_intent,
            result=transaction_result,
        )

    def store_get_results(self):
        return ()

    def store_get_requests(self):
        return (self.request,) if self.request is not None else ()

    def store_items(self):
        return (self.item,) if self.item is not None else ()

    def store_put_intents(self):
        return (self.intent,) if self.intent is not None else ()

    @contextmanager
    def transaction(self):
        yield self.uow


def test_commit_get_returns_result_committed_by_competing_transaction():
    request = _request("pick", "store")
    item = _item("item", "store")
    race_result = StoreGetResult("pick", "store", item, NOW, request.sequence)
    persistence = _CommitGetRacePersistence(
        request=request,
        item=item,
        transaction_request=None,
        transaction_item=item,
        transaction_result=race_result,
    )

    actual = DurableStoreManager(persistence).commit_get(
        "pick",
        StoreItem("item", "store", item.value),
        completed_at=NOW,
    )

    assert actual == race_result


def test_commit_get_rejects_changed_request_without_competing_result():
    request = _request("pick", "store")
    item = _item("item", "store")
    persistence = _CommitGetRacePersistence(
        request=request,
        item=item,
        transaction_request=None,
        transaction_item=item,
    )

    with pytest.raises(RuntimeError, match="store get request changed"):
        DurableStoreManager(persistence).commit_get(
            "pick",
            StoreItem("item", "store", item.value),
            completed_at=NOW,
        )


def test_commit_get_rejects_item_disappearing_inside_transaction():
    request = _request("pick", "store")
    item = _item("item", "store")
    persistence = _CommitGetRacePersistence(
        request=request,
        item=item,
        transaction_request=request,
        transaction_item=None,
        transaction_intent=None,
    )

    with pytest.raises(RuntimeError, match="store item disappeared during consume"):
        DurableStoreManager(persistence).commit_get(
            "pick",
            StoreItem("item", "store", item.value),
            completed_at=NOW,
        )


def test_commit_get_can_consume_still_pending_put_intent():
    request = _request("pick", "store", sequence=2)
    intent = _intent("item", "store", sequence=1, value={"v": 1})
    persistence = _CommitGetRacePersistence(
        request=request,
        intent=intent,
        transaction_request=request,
        transaction_intent=intent,
    )
    manager = DurableStoreManager(persistence)

    result = manager.commit_get(
        "pick",
        StoreItem("item", "store", {"v": 1}),
        completed_at=NOW,
    )

    assert result.item == _item("item", "store", sequence=1, value={"v": 1})
    assert persistence.uow.deleted_items == []
    assert persistence.uow.deleted_intents == ["item"]
    assert persistence.uow.saved_results == [result]
    assert persistence.uow.deleted_requests == ["pick"]


def test_ensure_selection_rejects_pending_identity_reuse_with_other_filter():
    filters = {
        "one": lambda item: item.value == 1,
        "two": lambda item: item.value == 2,
    }
    _, manager, backend = _runtime(kind="filter", filters=filters)

    assert manager.ensure_selection(
        backend,
        store_name="store",
        request_id="pick",
        requested_at=NOW,
        filter_key="one",
    ) is None

    with pytest.raises(RuntimeError, match="does not match requested store/filter"):
        manager.ensure_selection(
            backend,
            store_name="store",
            request_id="pick",
            requested_at=NOW,
            filter_key="two",
        )



def test_ensure_put_supports_backend_without_run_until():
    intent = _intent("pending", "store", value=9)
    persistence = _SnapshotPersistence(
        definitions=(StoreDefinition("store"),),
        intents=(intent,),
    )
    manager = DurableStoreManager(persistence)

    assert manager.ensure_put(
        SimpleNamespace(),
        store_name="store",
        item_id="pending",
        value=9,
        requested_at=NOW,
    ) is None


def test_ensure_selection_supports_backend_without_run_until():
    request = _request("pick", "store")
    persistence = _SnapshotPersistence(
        definitions=(StoreDefinition("store"),),
        requests=(request,),
    )
    manager = DurableStoreManager(persistence)

    assert manager.ensure_selection(
        SimpleNamespace(),
        store_name="store",
        request_id="pick",
        requested_at=NOW,
    ) is None

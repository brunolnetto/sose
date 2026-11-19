"""SimPy Store operations of ``SimPyBackend``, split from ``sose.backends.simpy``."""

from __future__ import annotations

import math
from collections.abc import Callable
from dataclasses import dataclass, field

from simpy.events import Event
from simpy.resources.store import FilterStore, PriorityStore, Store

from .base import StoreItem, StoreRequest, StoreSnapshot


@dataclass(order=True, slots=True)
class _PriorityEnvelope:
    priority: int
    sequence: int
    item: StoreItem = field(compare=False)


@dataclass(slots=True)
class _StoreState:
    store: Store
    kind: str
    items: dict[str, StoreItem]


class _SimPyStoreOperations:
    """FIFO, priority and filter Store methods mixed into ``SimPyBackend``."""

    def create_store(self, name: str, *, capacity: int | None = None) -> None:
        self._create_store(name, kind="fifo", capacity=capacity)

    def create_priority_store(self, name: str, *, capacity: int | None = None) -> None:
        self._create_store(name, kind="priority", capacity=capacity)

    def create_filter_store(self, name: str, *, capacity: int | None = None) -> None:
        self._create_store(name, kind="filter", capacity=capacity)

    def put_store(
        self,
        name: str,
        *,
        item_id: str,
        value: object,
        priority: int = 100,
        on_stored: Callable[[StoreItem], None] | None = None,
    ) -> StoreItem:
        state = self._store(name)
        if not item_id:
            raise ValueError("item_id cannot be empty")
        if any(item_id in current.items for current in self._stores.values()):
            raise ValueError(f"store item already exists: {item_id}")

        item = StoreItem(
            item_id=item_id,
            store_name=name,
            value=value,
            priority=priority,
        )
        state.items[item_id] = item

        native_item: object = item
        if state.kind == "priority":
            self._store_sequence += 1
            native_item = _PriorityEnvelope(priority, self._store_sequence, item)

        event = state.store.put(native_item)
        if on_stored is not None:
            if event.callbacks is None:  # pragma: no cover - defensive SimPy boundary
                raise RuntimeError("store put was processed before callback attachment")
            event.callbacks.append(lambda _: on_stored(item))
        return item

    def get_store(
        self,
        name: str,
        *,
        request_id: str,
        on_received: Callable[[StoreItem], None],
        filter: Callable[[StoreItem], bool] | None = None,
    ) -> StoreRequest:
        state = self._store(name)
        if not request_id:
            raise ValueError("request_id cannot be empty")
        if request_id in self._store_request_ids:
            raise ValueError(f"store request already exists: {request_id}")

        if filter is not None and state.kind != "filter":
            raise ValueError("filters are supported only by filter stores")

        self._store_request_ids.add(request_id)
        public_request = StoreRequest(
            request_id=request_id,
            store_name=name,
            requested_at=self.now,
        )

        if filter is not None:
            event = state.store.get(filter=lambda native: filter(
                native.item if isinstance(native, _PriorityEnvelope) else native
            ))
        else:
            event = state.store.get()

        def received(completed: Event) -> None:
            native_item = completed.value
            item = native_item.item if isinstance(native_item, _PriorityEnvelope) else native_item
            if not isinstance(item, StoreItem):
                raise RuntimeError("store returned a non-SOSE item")
            state.items.pop(item.item_id, None)
            on_received(item)

        if event.callbacks is None:  # pragma: no cover - defensive SimPy boundary
            raise RuntimeError("store get was processed before callback attachment")
        event.callbacks.append(received)
        return public_request

    def store_snapshot(self, name: str) -> StoreSnapshot:
        state = self._store(name)
        capacity = None if math.isinf(float(state.store.capacity)) else int(state.store.capacity)
        return StoreSnapshot(
            name=name,
            capacity=capacity,
            size=len(state.store.items),
            queued_puts=len(state.store.put_queue),
            queued_gets=len(state.store.get_queue),
        )

    def _create_store(self, name: str, *, kind: str, capacity: int | None) -> None:
        if not name:
            raise ValueError("store name cannot be empty")
        if capacity is not None and capacity < 1:
            raise ValueError("store capacity must be >= 1")
        if name in self._stores:
            raise ValueError(f"store already exists: {name}")

        kwargs = {} if capacity is None else {"capacity": capacity}
        if kind == "fifo":
            store = Store(self._env, **kwargs)
        elif kind == "priority":
            store = PriorityStore(self._env, **kwargs)
        elif kind == "filter":
            store = FilterStore(self._env, **kwargs)
        else:  # pragma: no cover - internal invariant
            raise ValueError(f"unknown store kind: {kind}")
        self._stores[name] = _StoreState(store=store, kind=kind, items={})

    def _store(self, name: str) -> _StoreState:
        try:
            return self._stores[name]
        except KeyError as exc:
            raise KeyError(f"unknown store: {name}") from exc

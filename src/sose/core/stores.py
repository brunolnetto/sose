from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import datetime

from sose.backends.base import StoreItem
from sose.core.runtime import (
    DurableStoreItem,
    StoreDefinition,
    StoreGetRequest,
    StoreGetResult,
    StorePutIntent,
)
from sose.core.stores_queries import StoreFilter, _StoreDurableQueries
from sose.core.stores_rebuild import _StoreRebuild
from sose.persistence.base import Persistence


class DurableStoreManager(_StoreRebuild, _StoreDurableQueries):
    """Durable Store truth reconstructed into an ephemeral StoreBackend."""

    def __init__(
        self,
        persistence: Persistence,
        *,
        filters: Mapping[str, StoreFilter] | None = None,
    ) -> None:
        self._persistence = persistence
        self._filters = dict(filters or {})

    def define(self, definition: StoreDefinition) -> None:
        with self._persistence.transaction() as uow:
            uow.save_store_definition(definition)

    def put(
        self,
        backend,
        *,
        store_name: str,
        item_id: str,
        value: object,
        requested_at: datetime,
        priority: int = 100,
        on_stored: Callable[[DurableStoreItem], None] | None = None,
    ) -> StorePutIntent:
        self._definition(store_name)
        if any(item.item_id == item_id for item in self._persistence.store_items()) or any(
            intent.item_id == item_id for intent in self._persistence.store_put_intents()
        ):
            raise ValueError(f"store item already exists: {item_id}")

        intent = StorePutIntent(
            item_id=item_id,
            store_name=store_name,
            value=value,
            priority=priority,
            requested_at=requested_at,
            sequence=self._next_sequence(),
        )
        with self._persistence.transaction() as uow:
            uow.save_store_put_intent(intent)

        self._submit_put_intent(backend, intent, on_stored=on_stored)
        return intent

    def ensure_put(
        self,
        backend,
        *,
        store_name: str,
        item_id: str,
        value: object,
        requested_at: datetime,
        priority: int = 100,
    ) -> DurableStoreItem | None:
        """Ensure one logical Store item exists without duplicating retries."""
        existing = self._item_with_id(item_id)
        if existing is not None:
            self._assert_store_item_identity(
                existing,
                store_name=store_name,
                value=value,
                item_id=item_id,
            )
            return existing

        consumed = self._consumed_item_with_id(item_id)
        if consumed is not None:
            self._assert_store_item_identity(
                consumed,
                store_name=store_name,
                value=value,
                item_id=item_id,
            )
            return consumed

        self._ensure_pending_put_intent(
            backend,
            store_name=store_name,
            item_id=item_id,
            value=value,
            requested_at=requested_at,
            priority=priority,
        )

        run_until = getattr(backend, "run_until", None)
        if callable(run_until):
            run_until(getattr(backend, "now", requested_at))
        return self._item_with_id(item_id)

    def get(
        self,
        backend,
        *,
        store_name: str,
        request_id: str,
        requested_at: datetime,
        filter_key: str | None = None,
        on_received: Callable[[DurableStoreItem], None] | None = None,
    ) -> StoreGetRequest:
        definition = self._definition(store_name)
        if any(
            request.request_id == request_id
            for request in self._persistence.store_get_requests()
        ) or any(
            result.request_id == request_id
            for result in self._persistence.store_get_results()
        ):
            raise ValueError(f"store get request already exists: {request_id}")
        if filter_key is not None and definition.kind != "filter":
            raise ValueError("filter_key is supported only for filter stores")
        if definition.kind == "filter" and filter_key is not None:
            self._filter(filter_key)

        request = StoreGetRequest(
            request_id=request_id,
            store_name=store_name,
            requested_at=requested_at,
            sequence=self._next_sequence(),
            filter_key=filter_key,
        )
        with self._persistence.transaction() as uow:
            uow.save_store_get_request(request)

        self._submit_get_request(backend, request, on_received=on_received)
        return request

    def commit_put(self, item_id: str) -> DurableStoreItem | None:
        existing = next(
            (item for item in self._persistence.store_items() if item.item_id == item_id),
            None,
        )
        if existing is not None:
            return existing

        intent = next(
            (
                intent
                for intent in self._persistence.store_put_intents()
                if intent.item_id == item_id
            ),
            None,
        )
        if intent is None:
            return None

        item = DurableStoreItem(
            item_id=intent.item_id,
            store_name=intent.store_name,
            value=intent.value,
            priority=intent.priority,
            sequence=intent.sequence,
        )
        with self._persistence.transaction() as uow:
            persisted = uow.get_store_put_intent(item_id)
            if persisted != intent:
                raise RuntimeError(f"store put intent changed before commit: {item_id}")
            uow.delete_store_put_intent(item_id)
            uow.save_store_item(item)
        return item

    def commit_get(
        self,
        request_id: str,
        backend_item: StoreItem,
        *,
        completed_at: datetime,
    ) -> StoreGetResult:
        existing_result = self.selection(request_id)
        if existing_result is not None:
            return existing_result

        request = self.pending_get(request_id)
        if request is None:
            raise KeyError(f"unknown store get request: {request_id}")
        if backend_item.store_name != request.store_name:
            raise RuntimeError(
                f"store get returned item from wrong store: {backend_item.item_id}"
            )

        item = self._item_with_id(backend_item.item_id)
        intent = self._pending_put_intent(backend_item.item_id)
        if item is None and intent is None:
            # Another worker may have committed this *exact* selection after
            # our first selection() read but before the durable item lookup.
            # The ephemeral SimPy callback is stale; authoritative receipt
            # identity is the idempotence oracle, not backend queue contents.
            winner = self.selection(request_id)
            if winner is not None:
                return winner
            # A different request consuming the same item is NOT a replay.
            raise KeyError(f"unknown durable store item: {backend_item.item_id}")

        consumed = self._consume_store_item(
            item=item,
            intent=intent,
            item_id=backend_item.item_id,
        )
        result = StoreGetResult(
            request_id=request.request_id,
            store_name=request.store_name,
            item=consumed,
            completed_at=completed_at,
            sequence=request.sequence,
        )

        with self._persistence.transaction() as uow:
            persisted_request = uow.get_store_get_request(request_id)
            if persisted_request != request:
                existing = uow.get_store_get_result(request_id)
                if existing is not None:
                    return existing
                raise RuntimeError(f"store get request changed: {request_id}")
            self._consume_persisted_store_item(
                uow,
                backend_item.item_id,
            )
            uow.save_store_get_result(result)
            uow.delete_store_get_request(request_id)
        return result

    def _ensure_pending_put_intent(
        self,
        backend,
        *,
        store_name: str,
        item_id: str,
        value: object,
        requested_at: datetime,
        priority: int,
    ) -> None:
        pending = self._pending_put_intent(item_id)
        if pending is None:
            self.put(
                backend,
                store_name=store_name,
                item_id=item_id,
                value=value,
                priority=priority,
                requested_at=requested_at,
            )
            return
        if (
            pending.store_name != store_name
            or pending.value != value
            or pending.priority != priority
        ):
            raise ValueError(f"store item identity conflict: {item_id}")

    def _submit_put_intent(
        self,
        backend,
        intent: StorePutIntent,
        *,
        on_stored: Callable[[DurableStoreItem], None] | None = None,
    ) -> None:
        def stored(_: StoreItem) -> None:
            item = self.commit_put(intent.item_id)
            if item is not None and on_stored is not None:
                on_stored(item)

        backend.put_store(
            intent.store_name,
            item_id=intent.item_id,
            value=intent.value,
            priority=intent.priority,
            on_stored=stored,
        )

    def _submit_get_request(
        self,
        backend,
        request: StoreGetRequest,
        *,
        on_received: Callable[[DurableStoreItem], None] | None = None,
    ) -> None:
        predicate = self._filter(request.filter_key) if request.filter_key is not None else None

        def received(item: StoreItem) -> None:
            result = self.commit_get(
                request.request_id,
                item,
                completed_at=backend.now,
            )
            if on_received is not None:
                on_received(result.item)

        backend.get_store(
            request.store_name,
            request_id=request.request_id,
            filter=predicate,
            on_received=received,
        )

    def ensure_selection(
        self,
        backend,
        *,
        store_name: str,
        request_id: str,
        requested_at: datetime,
        filter_key: str | None = None,
    ) -> StoreGetResult | None:
        """Ensure one durable selection request exists and return its result.

        FIFO/Priority/Filter choice remains a property of the Store definition
        and backend. Callers remain responsible for domain eligibility (for
        example validating a priority head before beginning dispatch).
        """
        existing = self.selection(request_id)
        if existing is not None:
            if existing.store_name != store_name:
                raise RuntimeError(
                    f"selection {request_id} belongs to {existing.store_name}, "
                    f"not {store_name}"
                )
            return existing

        pending = self.pending_get(request_id)
        if pending is not None:
            if pending.store_name != store_name or pending.filter_key != filter_key:
                raise RuntimeError(
                    f"pending selection {request_id} does not match requested store/filter"
                )
        else:
            self.get(
                backend,
                store_name=store_name,
                request_id=request_id,
                requested_at=requested_at,
                filter_key=filter_key,
            )

        run_until = getattr(backend, "run_until", None)
        if callable(run_until):
            run_until(getattr(backend, "now", requested_at))
        return self.selection(request_id)

from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import datetime

from sose.backends.base import StoreItem
from sose.core.runtime import (
    DurableStoreItem,
    StoreDefinition,
    StoreGetRequest,
    StorePutIntent,
)
from sose.persistence.base import Persistence


StoreFilter = Callable[[StoreItem], bool]


class DurableStoreManager:
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

    def validate_rebuild(self) -> None:
        definitions = {
            definition.name: definition
            for definition in self._persistence.store_definitions()
        }
        for item in self._persistence.store_items():
            if item.store_name not in definitions:
                raise RuntimeError(f"store item references unknown definition: {item.item_id}")
        for intent in self._persistence.store_put_intents():
            if intent.store_name not in definitions:
                raise RuntimeError(f"store put references unknown definition: {intent.item_id}")
        for request in self._persistence.store_get_requests():
            definition = definitions.get(request.store_name)
            if definition is None:
                raise RuntimeError(
                    f"store get references unknown definition: {request.request_id}"
                )
            if request.filter_key is not None:
                if definition.kind != "filter":
                    raise RuntimeError(
                        f"filtered get targets non-filter store: {request.request_id}"
                    )
                self._filter(request.filter_key)

        for definition in definitions.values():
            if definition.capacity is None:
                continue
            count = sum(
                item.store_name == definition.name
                for item in self._persistence.store_items()
            )
            if count > definition.capacity:
                raise RuntimeError(
                    f"durable store {definition.name} contains more items than capacity"
                )

    def rebuild_backend(self, backend) -> int:
        self.validate_rebuild()
        definitions = {
            definition.name: definition
            for definition in self._persistence.store_definitions()
        }
        for definition in definitions.values():
            self._create_backend_store(backend, definition)

        restored = 0
        for definition in definitions.values():
            items = [
                item
                for item in self._persistence.store_items()
                if item.store_name == definition.name
            ]
            for item in self._ordered_items(definition, items):
                backend.put_store(
                    definition.name,
                    item_id=item.item_id,
                    value=item.value,
                    priority=item.priority,
                )
                restored += 1

        pending = [
            *((intent.sequence, "put", intent) for intent in self._persistence.store_put_intents()),
            *((request.sequence, "get", request) for request in self._persistence.store_get_requests()),
        ]
        for _, operation, record in sorted(
            pending,
            key=lambda value: (value[0], 0 if value[1] == "put" else 1),
        ):
            if operation == "put":
                self._submit_put_intent(backend, record)
            else:
                self._submit_get_request(backend, record)
            restored += 1
        return restored

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

    def commit_get(self, request_id: str, backend_item: StoreItem) -> DurableStoreItem:
        request = next(
            (
                request
                for request in self._persistence.store_get_requests()
                if request.request_id == request_id
            ),
            None,
        )
        if request is None:
            raise KeyError(f"unknown store get request: {request_id}")
        if backend_item.store_name != request.store_name:
            raise RuntimeError(
                f"store get returned item from wrong store: {backend_item.item_id}"
            )

        item = next(
            (
                item
                for item in self._persistence.store_items()
                if item.item_id == backend_item.item_id
            ),
            None,
        )
        intent = next(
            (
                intent
                for intent in self._persistence.store_put_intents()
                if intent.item_id == backend_item.item_id
            ),
            None,
        )
        if item is None and intent is None:
            raise KeyError(f"unknown durable store item: {backend_item.item_id}")

        consumed = item or DurableStoreItem(
            item_id=intent.item_id,
            store_name=intent.store_name,
            value=intent.value,
            priority=intent.priority,
            sequence=intent.sequence,
        )

        with self._persistence.transaction() as uow:
            persisted_request = uow.get_store_get_request(request_id)
            if persisted_request != request:
                raise RuntimeError(f"store get request changed: {request_id}")

            persisted_item = uow.get_store_item(backend_item.item_id)
            persisted_intent = uow.get_store_put_intent(backend_item.item_id)
            if persisted_item is None and persisted_intent is None:
                raise RuntimeError(
                    f"store item disappeared during consume: {backend_item.item_id}"
                )
            if persisted_item is not None:
                uow.delete_store_item(backend_item.item_id)
            if persisted_intent is not None:
                uow.delete_store_put_intent(backend_item.item_id)
            uow.delete_store_get_request(request_id)
        return consumed

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
            consumed = self.commit_get(request.request_id, item)
            if on_received is not None:
                on_received(consumed)

        backend.get_store(
            request.store_name,
            request_id=request.request_id,
            filter=predicate,
            on_received=received,
        )

    def _definition(self, name: str) -> StoreDefinition:
        definition = next(
            (
                definition
                for definition in self._persistence.store_definitions()
                if definition.name == name
            ),
            None,
        )
        if definition is None:
            raise KeyError(f"unknown store definition: {name}")
        return definition

    def _filter(self, key: str) -> StoreFilter:
        try:
            return self._filters[key]
        except KeyError as exc:
            raise KeyError(f"unknown durable store filter: {key}") from exc

    def _next_sequence(self) -> int:
        return max(
            [
                *(item.sequence for item in self._persistence.store_items()),
                *(intent.sequence for intent in self._persistence.store_put_intents()),
                *(request.sequence for request in self._persistence.store_get_requests()),
            ],
            default=0,
        ) + 1

    @staticmethod
    def _ordered_items(
        definition: StoreDefinition,
        items: list[DurableStoreItem],
    ) -> list[DurableStoreItem]:
        if definition.kind == "priority":
            return sorted(items, key=lambda item: (item.priority, item.sequence, item.item_id))
        return sorted(items, key=lambda item: (item.sequence, item.item_id))

    @staticmethod
    def _create_backend_store(backend, definition: StoreDefinition) -> None:
        kwargs = {} if definition.capacity is None else {"capacity": definition.capacity}
        if definition.kind == "fifo":
            backend.create_store(definition.name, **kwargs)
        elif definition.kind == "priority":
            backend.create_priority_store(definition.name, **kwargs)
        else:
            backend.create_filter_store(definition.name, **kwargs)

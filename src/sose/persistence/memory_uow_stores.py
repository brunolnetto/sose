"""Store and container ledger methods of the in-memory unit of work.

Split from ``memory.py`` (``MemoryUnitOfWork``).
"""

from __future__ import annotations

from copy import deepcopy
from typing import TYPE_CHECKING

from sose.core.runtime import (
    ContainerDefinition,
    ContainerOperationIntent,
    ContainerOperationResult,
    ContainerState,
    DurableStoreItem,
    StoreDefinition,
    StoreGetRequest,
    StoreGetResult,
    StorePutIntent,
)

if TYPE_CHECKING:
    from .memory import _State


class StoreContainerRecordsUnitOfWorkMixin:
    """Staged reads/writes of store and container ledger records."""

    _working: _State

    def get_store_item(self, item_id: str) -> DurableStoreItem | None:
        value = self._working.store_items.get(item_id)
        return deepcopy(value) if value else None

    def get_store_put_intent(self, item_id: str) -> StorePutIntent | None:
        value = self._working.store_put_intents.get(item_id)
        return deepcopy(value) if value else None

    def get_store_get_request(self, request_id: str) -> StoreGetRequest | None:
        value = self._working.store_get_requests.get(request_id)
        return deepcopy(value) if value else None

    def get_store_get_result(self, request_id: str) -> StoreGetResult | None:
        value = self._working.store_get_results.get(request_id)
        return deepcopy(value) if value else None

    def save_store_definition(self, definition: StoreDefinition) -> None:
        existing = self._working.store_definitions.get(definition.name)
        if existing is not None and existing != definition:
            raise ValueError(f"store definition already exists: {definition.name}")
        self._working.store_definitions[definition.name] = deepcopy(definition)
        self._mark_dirty("store_definitions", definition.name)

    def save_store_item(self, item: DurableStoreItem) -> None:
        if item.store_name not in self._working.store_definitions:
            raise KeyError(f"unknown store definition: {item.store_name}")
        if item.item_id in self._working.store_put_intents:
            raise ValueError(f"store item is still pending put: {item.item_id}")
        existing = self._working.store_items.get(item.item_id)
        if existing is not None and existing != item:
            raise ValueError(f"store item already exists: {item.item_id}")
        self._working.store_items[item.item_id] = deepcopy(item)
        self._mark_dirty("store_items", item.item_id)

    def delete_store_item(self, item_id: str) -> None:
        self._working.store_items.pop(item_id, None)
        self._mark_dirty("store_items", item_id)

    def save_store_put_intent(self, intent: StorePutIntent) -> None:
        if intent.store_name not in self._working.store_definitions:
            raise KeyError(f"unknown store definition: {intent.store_name}")
        if intent.item_id in self._working.store_items:
            raise ValueError(f"store item already accepted: {intent.item_id}")
        existing = self._working.store_put_intents.get(intent.item_id)
        if existing is not None and existing != intent:
            raise ValueError(f"store put intent already exists: {intent.item_id}")
        self._working.store_put_intents[intent.item_id] = deepcopy(intent)
        self._mark_dirty("store_put_intents", intent.item_id)

    def delete_store_put_intent(self, item_id: str) -> None:
        self._working.store_put_intents.pop(item_id, None)
        self._mark_dirty("store_put_intents", item_id)

    def save_store_get_request(self, request: StoreGetRequest) -> None:
        if request.store_name not in self._working.store_definitions:
            raise KeyError(f"unknown store definition: {request.store_name}")
        if request.request_id in self._working.store_get_results:
            raise ValueError(f"store get request already completed: {request.request_id}")
        existing = self._working.store_get_requests.get(request.request_id)
        if existing is not None and existing != request:
            raise ValueError(f"store get request already exists: {request.request_id}")
        self._working.store_get_requests[request.request_id] = deepcopy(request)
        self._mark_dirty("store_get_requests", request.request_id)

    def delete_store_get_request(self, request_id: str) -> None:
        self._working.store_get_requests.pop(request_id, None)
        self._mark_dirty("store_get_requests", request_id)

    def save_store_get_result(self, result: StoreGetResult) -> None:
        if result.store_name not in self._working.store_definitions:
            raise KeyError(f"unknown store definition: {result.store_name}")
        existing = self._working.store_get_results.get(result.request_id)
        if existing is not None:
            if existing != result:
                raise ValueError(f"store get result already exists: {result.request_id}")
            return
        request = self._working.store_get_requests.get(result.request_id)
        if request is None:
            raise KeyError(f"unknown store get request: {result.request_id}")
        if request.store_name != result.store_name:
            raise ValueError(f"store get result targets wrong store: {result.request_id}")
        self._working.store_get_results[result.request_id] = deepcopy(result)
        self._mark_dirty("store_get_results", result.request_id)

    def get_container_state(self, name: str) -> ContainerState | None:
        value = self._working.container_states.get(name)
        return deepcopy(value) if value else None

    def get_container_operation_intent(self, request_id: str) -> ContainerOperationIntent | None:
        value = self._working.container_operation_intents.get(request_id)
        return deepcopy(value) if value else None

    def get_container_operation_result(self, request_id: str) -> ContainerOperationResult | None:
        value = self._working.container_operation_results.get(request_id)
        return deepcopy(value) if value else None

    def save_container_definition(self, definition: ContainerDefinition) -> None:
        existing = self._working.container_definitions.get(definition.name)
        if existing is not None and existing != definition:
            raise ValueError(f"container definition already exists: {definition.name}")
        self._working.container_definitions[definition.name] = deepcopy(definition)
        self._mark_dirty("container_definitions", definition.name)

    def save_container_state(self, state: ContainerState) -> None:
        definition = self._working.container_definitions.get(state.name)
        if definition is None:
            raise KeyError(f"unknown container definition: {state.name}")
        if state.level > definition.capacity:
            raise ValueError(f"container level exceeds capacity: {state.name}")
        self._working.container_states[state.name] = deepcopy(state)
        self._mark_dirty("container_states", state.name)

    def save_container_operation_intent(self, intent: ContainerOperationIntent) -> None:
        if intent.container_name not in self._working.container_definitions:
            raise KeyError(f"unknown container definition: {intent.container_name}")
        if intent.request_id in self._working.container_operation_results:
            raise ValueError(f"container request already completed: {intent.request_id}")
        existing = self._working.container_operation_intents.get(intent.request_id)
        if existing is not None and existing != intent:
            raise ValueError(f"container request already exists: {intent.request_id}")
        self._working.container_operation_intents[intent.request_id] = deepcopy(intent)
        self._mark_dirty("container_operation_intents", intent.request_id)

    def delete_container_operation_intent(self, request_id: str) -> None:
        self._working.container_operation_intents.pop(request_id, None)
        self._mark_dirty("container_operation_intents", request_id)

    def save_container_operation_result(self, result: ContainerOperationResult) -> None:
        existing = self._working.container_operation_results.get(result.request_id)
        if existing is not None:
            if existing != result:
                raise ValueError(f"container result already exists: {result.request_id}")
            return
        intent = self._working.container_operation_intents.get(result.request_id)
        if intent is None:
            raise KeyError(f"unknown container operation intent: {result.request_id}")
        if intent.container_name != result.container_name or intent.operation != result.operation:
            raise ValueError(f"container result does not match intent: {result.request_id}")
        self._working.container_operation_results[result.request_id] = deepcopy(result)
        self._mark_dirty("container_operation_results", result.request_id)

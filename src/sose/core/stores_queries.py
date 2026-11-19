"""Durable record lookups and consume helpers of ``DurableStoreManager``.

Split from ``sose.core.stores``.
"""

from __future__ import annotations

from collections.abc import Callable

from sose.backends.base import StoreItem
from sose.core.runtime import (
    DurableStoreItem,
    StoreDefinition,
    StoreGetRequest,
    StoreGetResult,
    StorePutIntent,
)

StoreFilter = Callable[[StoreItem], bool]


class _StoreDurableQueries:
    """Read-only lookups over durable Store records plus consume helpers."""

    @staticmethod
    def _assert_store_item_identity(
        item: DurableStoreItem,
        *,
        store_name: str,
        value: object,
        item_id: str,
    ) -> None:
        if item.store_name == store_name and item.value == value:
            return
        raise ValueError(f"store item identity conflict: {item_id}")

    @staticmethod
    def _consume_store_item(
        *,
        item: DurableStoreItem | None,
        intent: StorePutIntent | None,
        item_id: str,
    ) -> DurableStoreItem:
        if item is not None:
            return item
        if intent is None:  # pragma: no cover - guarded by caller
            raise KeyError(f"unknown durable store item: {item_id}")
        return DurableStoreItem(
            item_id=intent.item_id,
            store_name=intent.store_name,
            value=intent.value,
            priority=intent.priority,
            sequence=intent.sequence,
        )

    @staticmethod
    def _consume_persisted_store_item(uow, item_id: str) -> None:
        persisted_item = uow.get_store_item(item_id)
        persisted_intent = uow.get_store_put_intent(item_id)
        if persisted_item is None and persisted_intent is None:
            raise RuntimeError(
                f"store item disappeared during consume: {item_id}"
            )
        if persisted_item is not None:
            uow.delete_store_item(item_id)
        if persisted_intent is not None:
            uow.delete_store_put_intent(item_id)

    def _item_with_id(self, item_id: str) -> DurableStoreItem | None:
        return next(
            (item for item in self._persistence.store_items() if item.item_id == item_id),
            None,
        )

    def _consumed_item_with_id(self, item_id: str) -> DurableStoreItem | None:
        return next(
            (
                result.item
                for result in self._persistence.store_get_results()
                if result.item.item_id == item_id
            ),
            None,
        )

    def _pending_put_intent(self, item_id: str) -> StorePutIntent | None:
        return next(
            (
                intent
                for intent in self._persistence.store_put_intents()
                if intent.item_id == item_id
            ),
            None,
        )

    def pending_get(self, request_id: str) -> StoreGetRequest | None:
        """Return durable pending selection intent for one request."""
        return next(
            (
                request
                for request in self._persistence.store_get_requests()
                if request.request_id == request_id
            ),
            None,
        )

    def selection(self, request_id: str) -> StoreGetResult | None:
        """Return committed durable selection ownership, if any.

        A committed StoreGetResult remains authoritative after the selected item
        has disappeared from the store. Recovery should continue from this result
        rather than attempting to rediscover or consume the original item.
        """
        return next(
            (
                result
                for result in self._persistence.store_get_results()
                if result.request_id == request_id
            ),
            None,
        )

    def result(self, request_id: str) -> StoreGetResult | None:
        """Backward-compatible alias for committed selection ownership."""
        return self.selection(request_id)

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
                *(result.sequence for result in self._persistence.store_get_results()),
            ],
            default=0,
        ) + 1

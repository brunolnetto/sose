"""Rebuild validation and backend reconstruction of ``DurableStoreManager``.

Split from ``sose.core.stores``.
"""

from __future__ import annotations

from sose.core.runtime import DurableStoreItem, StoreDefinition


class _StoreRebuild:
    """Validate durable Store records and reconstruct them into a backend."""

    def validate_rebuild(self) -> None:
        definitions = {
            definition.name: definition
            for definition in self._persistence.store_definitions()
        }
        self._validate_store_records(definitions)
        self._validate_store_capacities(definitions)

    def _validate_store_records(self, definitions: dict[str, StoreDefinition]) -> None:
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

    def _validate_store_capacities(
        self,
        definitions: dict[str, StoreDefinition],
    ) -> None:
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
        self._create_stores_on_backend(backend, definitions)
        restored_items = self._restore_store_items(backend, definitions)
        restored_pending = self._restore_pending_operations(backend)
        return restored_items + restored_pending

    def _create_stores_on_backend(
        self,
        backend,
        definitions: dict[str, StoreDefinition],
    ) -> None:
        for definition in definitions.values():
            self._create_backend_store(backend, definition)

    def _restore_store_items(
        self,
        backend,
        definitions: dict[str, StoreDefinition],
    ) -> int:
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
        return restored

    def _restore_pending_operations(self, backend) -> int:
        pending = [
            *(
                (intent.sequence, "put", intent)
                for intent in self._persistence.store_put_intents()
            ),
            *(
                (request.sequence, "get", request)
                for request in self._persistence.store_get_requests()
            ),
        ]
        restored = 0
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

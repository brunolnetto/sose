from __future__ import annotations

from abc import abstractmethod
from datetime import datetime, timezone

import pytest

from sose.core.runtime import (
    ContainerDefinition,
    ContainerOperationIntent,
    ContainerState,
    DurableStoreItem,
    ResourceDefinition,
    ResourceDemand,
    StoreDefinition,
    StoreGetRequest,
    StoreGetResult,
)

NOW = datetime(2026, 1, 1, 8, tzinfo=timezone.utc)


class PersistenceConformanceSuite:
    """Reusable behavioral contract for every Persistence implementation."""

    @abstractmethod
    def make_persistence(self):
        raise NotImplementedError

    def test_transaction_commit_is_atomic_and_visible_after_exit(self):
        store = self.make_persistence()

        with store.transaction() as uow:
            uow.save_resource_definition(ResourceDefinition("bay", capacity=1))
            assert store.resource_definitions() == ()

        assert store.resource_definitions() == (ResourceDefinition("bay", capacity=1),)

    def test_transaction_exception_rolls_back_every_write(self):
        store = self.make_persistence()

        with pytest.raises(RuntimeError, match="abort"):
            with store.transaction() as uow:
                uow.save_resource_definition(ResourceDefinition("bay", capacity=1))
                uow.save_store_definition(StoreDefinition("inbox"))
                raise RuntimeError("abort")

        assert store.resource_definitions() == ()
        assert store.store_definitions() == ()

    def test_reads_do_not_alias_persisted_mutable_payloads(self):
        store = self.make_persistence()
        payload = {"nested": {"value": 1}}

        with store.transaction() as uow:
            uow.save_store_definition(StoreDefinition("inbox"))
            uow.save_store_item(
                DurableStoreItem(
                    item_id="item-1",
                    store_name="inbox",
                    value=payload,
                    priority=100,
                    sequence=1,
                )
            )

        payload["nested"]["value"] = 500
        assert store.store_items()[0].value == {"nested": {"value": 1}}

        returned = store.store_items()[0]
        returned.value["nested"]["value"] = 999

        assert store.store_items()[0].value == {"nested": {"value": 1}}

    def test_sequence_order_is_backend_independent(self):
        store = self.make_persistence()

        with store.transaction() as uow:
            uow.save_resource_definition(ResourceDefinition("bay", capacity=1))
            uow.save_resource_demand(
                ResourceDemand("later", "bay", 100, NOW, sequence=2)
            )
            uow.save_resource_demand(
                ResourceDemand("earlier", "bay", 100, NOW, sequence=1)
            )
            uow.save_container_definition(
                ContainerDefinition("fuel", capacity=10.0, initial=5.0)
            )
            uow.save_container_state(ContainerState("fuel", level=5.0))
            uow.save_container_operation_intent(
                ContainerOperationIntent(
                    "later-op", "fuel", "get", 1.0, NOW, sequence=2
                )
            )
            uow.save_container_operation_intent(
                ContainerOperationIntent(
                    "earlier-op", "fuel", "get", 1.0, NOW, sequence=1
                )
            )

        assert [d.request_id for d in store.resource_demands()] == [
            "earlier",
            "later",
        ]
        assert [i.request_id for i in store.container_operation_intents()] == [
            "earlier-op",
            "later-op",
        ]

        store_item_later = DurableStoreItem(
            "result-item-later", "inbox", 2, 100, sequence=4
        )
        store_item_earlier = DurableStoreItem(
            "result-item-earlier", "inbox", 1, 100, sequence=3
        )
        with store.transaction() as uow:
            uow.save_store_definition(StoreDefinition("inbox"))
            later_request = StoreGetRequest("get-later", "inbox", NOW, sequence=4)
            earlier_request = StoreGetRequest("get-earlier", "inbox", NOW, sequence=3)
            uow.save_store_get_request(later_request)
            uow.save_store_get_request(earlier_request)
            uow.save_store_get_result(
                StoreGetResult("get-later", "inbox", store_item_later, NOW, sequence=4)
            )
            uow.save_store_get_result(
                StoreGetResult("get-earlier", "inbox", store_item_earlier, NOW, sequence=3)
            )
            uow.delete_store_get_request("get-later")
            uow.delete_store_get_request("get-earlier")

        assert [r.request_id for r in store.store_get_results()] == [
            "get-earlier",
            "get-later",
        ]

    def test_identical_definition_save_is_idempotent(self):
        store = self.make_persistence()
        definition = StoreDefinition("inbox", capacity=2)

        with store.transaction() as uow:
            uow.save_store_definition(definition)

        with store.transaction() as uow:
            uow.save_store_definition(definition)

        assert store.store_definitions() == (definition,)

    def test_conflicting_definition_is_rejected_without_partial_commit(self):
        store = self.make_persistence()

        with store.transaction() as uow:
            uow.save_store_definition(StoreDefinition("inbox", capacity=1))

        with pytest.raises(ValueError, match="already exists"):
            with store.transaction() as uow:
                uow.save_store_definition(StoreDefinition("inbox", capacity=2))

        assert store.store_definitions() == (
            StoreDefinition("inbox", capacity=1),
        )

    def test_terminal_result_and_request_transition_is_atomic(self):
        store = self.make_persistence()
        item = DurableStoreItem(
            item_id="consumed",
            store_name="inbox",
            value={"sku": "x"},
            priority=100,
            sequence=1,
        )
        request = StoreGetRequest(
            request_id="get-1",
            store_name="inbox",
            requested_at=NOW,
            sequence=2,
        )
        result = StoreGetResult(
            request_id="get-1",
            store_name="inbox",
            item=item,
            completed_at=NOW,
            sequence=2,
        )

        with store.transaction() as uow:
            uow.save_store_definition(StoreDefinition("inbox"))
            uow.save_store_get_request(request)

        with store.transaction() as uow:
            uow.save_store_get_result(result)
            uow.delete_store_get_request(request.request_id)

        assert store.store_get_requests() == ()
        assert store.store_get_results() == (result,)

    def test_pending_to_terminal_failure_rolls_back_as_one_unit(self):
        store = self.make_persistence()
        item = DurableStoreItem(
            item_id="consumed",
            store_name="inbox",
            value=1,
            priority=100,
            sequence=1,
        )
        request = StoreGetRequest("get-rollback", "inbox", NOW, sequence=2)
        result = StoreGetResult("get-rollback", "inbox", item, NOW, sequence=2)

        with store.transaction() as uow:
            uow.save_store_definition(StoreDefinition("inbox"))
            uow.save_store_get_request(request)

        with pytest.raises(RuntimeError, match="abort transition"):
            with store.transaction() as uow:
                uow.save_store_get_result(result)
                uow.delete_store_get_request(request.request_id)
                raise RuntimeError("abort transition")

        assert store.store_get_requests() == (request,)
        assert store.store_get_results() == ()

    def test_completed_request_identity_cannot_be_reopened(self):
        store = self.make_persistence()
        item = DurableStoreItem(
            item_id="consumed",
            store_name="inbox",
            value=1,
            priority=100,
            sequence=1,
        )
        request = StoreGetRequest("get-1", "inbox", NOW, sequence=2)
        result = StoreGetResult("get-1", "inbox", item, NOW, sequence=2)

        with store.transaction() as uow:
            uow.save_store_definition(StoreDefinition("inbox"))
            uow.save_store_get_request(request)
            uow.save_store_get_result(result)
            uow.delete_store_get_request("get-1")

        with pytest.raises(ValueError, match="already completed"):
            with store.transaction() as uow:
                uow.save_store_get_request(request)

        assert store.store_get_requests() == ()
        assert store.store_get_results() == (result,)

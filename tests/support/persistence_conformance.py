from __future__ import annotations

from abc import abstractmethod
from datetime import datetime, timedelta, timezone

import pytest

from sose.core.events import Command, DomainEvent
from sose.domain.entity import Entity
from sose.core.runtime import (
    ContainerDefinition,
    ContainerOperationIntent,
    ContainerState,
    DurableStoreItem,
    PreemptiveResourceDefinition,
    PreemptiveResourceDemand,
    ResourceDefinition,
    ResourceDemand,
    ScheduledWork,
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

    def test_entity_round_trip_and_rollback_are_durable(self):
        store = self.make_persistence()
        entity = Entity(
            id="entity-1",
            entity_type="demo",
            state="planned",
            attributes={"nested": {"value": 1}},
            created_at=NOW,
            updated_at=NOW,
            version=1,
        )

        expected = Entity(
            id="entity-1",
            entity_type="demo",
            state="planned",
            attributes={"nested": {"value": 1}},
            created_at=NOW,
            updated_at=NOW,
            version=1,
        )

        with store.transaction() as uow:
            uow.save_entity(entity)

        entity.attributes["nested"]["value"] = 500
        with store.transaction() as uow:
            loaded = uow.get_entity("demo", "entity-1")
            assert loaded == expected
            assert loaded is not None
            loaded.attributes["nested"]["value"] = 999

        with store.transaction() as uow:
            assert uow.get_entity("demo", "entity-1") == expected

        changed = Entity(
            id="entity-1",
            entity_type="demo",
            state="running",
            attributes={"nested": {"value": 2}},
            created_at=NOW,
            updated_at=NOW,
            version=2,
        )
        with pytest.raises(RuntimeError, match="abort entity"):
            with store.transaction() as uow:
                uow.save_entity(changed)
                assert uow.get_entity("demo", "entity-1") == changed
                raise RuntimeError("abort entity")

        with store.transaction() as uow:
            assert uow.get_entity("demo", "entity-1") == expected

    def test_transaction_exception_rolls_back_every_write(self):
        store = self.make_persistence()
        event = DomainEvent(
            event_id="event-rollback",
            name="demo",
            entity_type="demo",
            entity_id="1",
            occurred_at=NOW,
            payload={"phase": "before-abort"},
        )

        with pytest.raises(RuntimeError, match="abort"):
            with store.transaction() as uow:
                uow.save_resource_definition(ResourceDefinition("bay", capacity=1))
                uow.save_store_definition(StoreDefinition("inbox"))
                uow.append_event(event)
                raise RuntimeError("abort")

        assert store.resource_definitions() == ()
        assert store.store_definitions() == ()
        assert store.events() == ()

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

    def test_event_payloads_are_isolated_on_write_and_read(self):
        store = self.make_persistence()
        payload = {"nested": {"value": 1}}
        event = DomainEvent(
            event_id="event-1",
            name="demo",
            entity_type="demo",
            entity_id="1",
            occurred_at=NOW,
            payload=payload,
        )

        with store.transaction() as uow:
            uow.append_event(event)

        payload["nested"]["value"] = 500
        assert store.events()[0].payload == {"nested": {"value": 1}}

        returned = store.events()[0]
        returned.payload["nested"]["value"] = 999

        assert store.events()[0].payload == {"nested": {"value": 1}}

    def test_commands_and_scheduled_work_are_atomic_isolated_and_ordered(self):
        store = self.make_persistence()
        payload = {"nested": {"value": 1}}
        later_command = Command(
            command_id="command-later",
            name="run",
            entity_type="demo",
            entity_id="entity-1",
            due_at=NOW + timedelta(hours=2),
            payload=payload,
        )
        earlier_command = Command(
            command_id="command-earlier",
            name="run",
            entity_type="demo",
            entity_id="entity-1",
            due_at=NOW + timedelta(hours=1),
            payload={"nested": {"value": 2}},
        )
        later_work = ScheduledWork(
            "work-later",
            later_command.due_at,
            priority=100,
            sequence=2,
            command_id=later_command.command_id,
        )
        earlier_work = ScheduledWork(
            "work-earlier",
            earlier_command.due_at,
            priority=100,
            sequence=1,
            command_id=earlier_command.command_id,
        )

        with store.transaction() as uow:
            uow.save_command(later_command)
            uow.save_command(earlier_command)
            uow.save_scheduled_work(later_work)
            uow.save_scheduled_work(earlier_work)
            assert uow.get_command(later_command.command_id) == later_command
            assert uow.get_scheduled_work(earlier_work.work_id) == earlier_work

        payload["nested"]["value"] = 500
        assert store.command(later_command.command_id).payload == {
            "nested": {"value": 1}
        }

        returned = store.command(later_command.command_id)
        assert returned is not None
        returned.payload["nested"]["value"] = 999
        assert store.command(later_command.command_id).payload == {
            "nested": {"value": 1}
        }

        assert store.scheduled_work() == (earlier_work, later_work)
        assert store.due_scheduled_work(NOW + timedelta(hours=1)) == (earlier_work,)

        rollback_command = Command(
            command_id="command-rollback",
            name="run",
            entity_type="demo",
            entity_id="entity-1",
            due_at=NOW + timedelta(hours=3),
            payload={"phase": "rollback"},
        )
        rollback_work = ScheduledWork(
            "work-rollback",
            rollback_command.due_at,
            priority=100,
            sequence=3,
            command_id=rollback_command.command_id,
        )
        with pytest.raises(RuntimeError, match="abort schedule"):
            with store.transaction() as uow:
                uow.save_command(rollback_command)
                uow.save_scheduled_work(rollback_work)
                assert uow.get_command(rollback_command.command_id) == rollback_command
                assert uow.get_scheduled_work(rollback_work.work_id) == rollback_work
                raise RuntimeError("abort schedule")

        assert store.command(rollback_command.command_id) is None
        assert store.scheduled_work() == (earlier_work, later_work)

    def test_preemptive_resource_demands_use_semantic_priority_and_sequence_order(self):
        store = self.make_persistence()

        with store.transaction() as uow:
            uow.save_preemptive_resource_definition(
                PreemptiveResourceDefinition("crew", capacity=1)
            )
            uow.save_preemptive_resource_demand(
                PreemptiveResourceDemand(
                    "later",
                    "crew",
                    priority=50,
                    preempt=False,
                    requested_at=NOW,
                    sequence=3,
                )
            )
            uow.save_preemptive_resource_demand(
                PreemptiveResourceDemand(
                    "urgent",
                    "crew",
                    priority=1,
                    preempt=True,
                    requested_at=NOW,
                    sequence=4,
                )
            )
            uow.save_preemptive_resource_demand(
                PreemptiveResourceDemand(
                    "earlier",
                    "crew",
                    priority=50,
                    preempt=False,
                    requested_at=NOW,
                    sequence=2,
                )
            )

        assert [d.request_id for d in store.preemptive_resource_demands()] == [
            "urgent",
            "earlier",
            "later",
        ]

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

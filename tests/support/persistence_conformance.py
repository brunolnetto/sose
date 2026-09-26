from __future__ import annotations

from abc import abstractmethod
from datetime import datetime, timedelta, timezone

import pytest

from sose.core.events import Command, DomainEvent
from sose.domain.entity import Entity
from sose.scenarios import AttributeEffect
from sose.scenarios.model import ScenarioActivation, ScenarioRuntimeState
from sose.core.runtime import (
    ContainerDefinition,
    ContainerOperationIntent,
    ContainerOperationResult,
    ContainerState,
    DurableStoreItem,
    PreemptiveResourceDefinition,
    PreemptiveResourceDemand,
    ResourceDefinition,
    ResourceDemand,
    ResourceReleaseIntent,
    ResourceReservation,
    ScheduledWork,
    SimulationPosition,
    StoreDefinition,
    StoreGetRequest,
    StoreGetResult,
    StorePutIntent,
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

        with pytest.raises(RuntimeError, match="abort consume"):
            with store.transaction() as uow:
                uow.delete_scheduled_work(earlier_work.work_id)
                uow.delete_command(earlier_command.command_id)
                raise RuntimeError("abort consume")

        assert store.command(earlier_command.command_id) == earlier_command
        assert store.scheduled_work() == (earlier_work, later_work)

        with store.transaction() as uow:
            uow.delete_scheduled_work(earlier_work.work_id)
            uow.delete_command(earlier_command.command_id)

        assert store.command(earlier_command.command_id) is None
        assert store.scheduled_work() == (later_work,)

    def test_recovery_checkpoints_are_atomic_and_isolated(self):
        store = self.make_persistence()
        effect_payload = {"nested": {"value": 1}}
        position = SimulationPosition(
            logical_time=NOW,
            logical_tick=7,
            execution_sequence=11,
            committed_sequence=9,
        )
        state = ScenarioRuntimeState(
            activations=(
                ScenarioActivation(
                    activation_id="activation-1",
                    scenario_name="checkpoint",
                    activated_at=NOW,
                    expires_at=NOW + timedelta(hours=1),
                    priority=100,
                    effects=(AttributeEffect("checkpoint.value", effect_payload),),
                    trigger_key=("tick", 7),
                ),
            ),
        )

        with store.transaction() as uow:
            uow.set_simulation_position(position)
            uow.set_scenario_state(state)

        effect_payload["nested"]["value"] = 500
        assert store.simulation_position() == position
        persisted_state = store.scenario_state()
        assert persisted_state is not None
        persisted_effect = persisted_state.activations[0].effects[0]
        assert isinstance(persisted_effect, AttributeEffect)
        assert persisted_effect.value == {"nested": {"value": 1}}

        persisted_effect.value["nested"]["value"] = 999
        reloaded_state = store.scenario_state()
        assert reloaded_state is not None
        reloaded_effect = reloaded_state.activations[0].effects[0]
        assert isinstance(reloaded_effect, AttributeEffect)
        assert reloaded_effect.value == {"nested": {"value": 1}}

        changed_position = SimulationPosition(
            logical_time=NOW + timedelta(hours=2),
            logical_tick=8,
            execution_sequence=12,
            committed_sequence=10,
        )
        changed_state = ScenarioRuntimeState()
        with pytest.raises(RuntimeError, match="abort checkpoint"):
            with store.transaction() as uow:
                uow.set_simulation_position(changed_position)
                uow.set_scenario_state(changed_state)
                raise RuntimeError("abort checkpoint")

        assert store.simulation_position() == position
        assert store.scenario_state() == state

    def test_store_put_intent_lifecycle_is_atomic_and_isolated(self):
        store = self.make_persistence()
        payload = {"nested": {"value": 1}}
        intent = StorePutIntent(
            item_id="pending-item",
            store_name="inbox",
            value=payload,
            priority=50,
            requested_at=NOW,
            sequence=1,
        )
        item = DurableStoreItem(
            item_id=intent.item_id,
            store_name=intent.store_name,
            value={"nested": {"value": 1}},
            priority=intent.priority,
            sequence=intent.sequence,
        )

        with store.transaction() as uow:
            uow.save_store_definition(StoreDefinition("inbox"))
            uow.save_store_put_intent(intent)
            assert uow.get_store_put_intent(intent.item_id) == intent

        payload["nested"]["value"] = 500
        assert store.store_put_intents()[0].value == {"nested": {"value": 1}}
        returned = store.store_put_intents()[0]
        returned.value["nested"]["value"] = 999
        assert store.store_put_intents()[0].value == {"nested": {"value": 1}}

        with pytest.raises(RuntimeError, match="abort put commit"):
            with store.transaction() as uow:
                uow.delete_store_put_intent(intent.item_id)
                uow.save_store_item(item)
                raise RuntimeError("abort put commit")

        assert store.store_put_intents() == (intent,)
        assert store.store_items() == ()

        with store.transaction() as uow:
            uow.delete_store_put_intent(intent.item_id)
            uow.save_store_item(item)

        assert store.store_put_intents() == ()
        assert store.store_items() == (item,)

    def test_container_completion_transition_is_atomic_and_terminal(self):
        store = self.make_persistence()
        definition = ContainerDefinition("fuel", capacity=10.0, initial=5.0)
        state = ContainerState("fuel", level=5.0)
        intent = ContainerOperationIntent(
            "consume",
            "fuel",
            "get",
            2.0,
            NOW,
            sequence=1,
        )
        result = ContainerOperationResult(
            request_id="consume",
            container_name="fuel",
            operation="get",
            amount=2.0,
            completed_at=NOW,
            level_before=5.0,
            level_after=3.0,
            sequence=1,
        )
        next_state = ContainerState("fuel", level=3.0)

        with store.transaction() as uow:
            uow.save_container_definition(definition)
            uow.save_container_state(state)
            uow.save_container_operation_intent(intent)

        with pytest.raises(RuntimeError, match="abort container completion"):
            with store.transaction() as uow:
                uow.save_container_state(next_state)
                uow.save_container_operation_result(result)
                uow.delete_container_operation_intent(intent.request_id)
                raise RuntimeError("abort container completion")

        assert store.container_states() == (state,)
        assert store.container_operation_intents() == (intent,)
        assert store.container_operation_results() == ()

        with store.transaction() as uow:
            uow.save_container_state(next_state)
            uow.save_container_operation_result(result)
            assert uow.get_container_operation_result(intent.request_id) == result
            uow.delete_container_operation_intent(intent.request_id)

        assert store.container_states() == (next_state,)
        assert store.container_operation_intents() == ()
        assert store.container_operation_results() == (result,)

        with pytest.raises(ValueError, match="already completed"):
            with store.transaction() as uow:
                uow.save_container_operation_intent(intent)

    def test_resource_demand_reservation_and_release_intent_lifecycle_is_atomic(self):
        store = self.make_persistence()
        definition = ResourceDefinition("bay", capacity=1)
        demand = ResourceDemand("request-1", "bay", 25, NOW, sequence=1)
        reservation = ResourceReservation(
            reservation_id="reservation-1",
            request_id=demand.request_id,
            resource_name=demand.resource_name,
            acquired_at=NOW,
            sequence=demand.sequence,
        )
        release = ResourceReleaseIntent(
            intent_id="release-1",
            reservation_id=reservation.reservation_id,
            resource_name=reservation.resource_name,
            requested_at=NOW,
        )

        with store.transaction() as uow:
            uow.save_resource_definition(definition)
            uow.save_resource_demand(demand)

        with pytest.raises(RuntimeError, match="abort grant"):
            with store.transaction() as uow:
                uow.delete_resource_demand(demand.request_id)
                uow.save_resource_reservation(reservation)
                raise RuntimeError("abort grant")

        assert store.resource_demands() == (demand,)
        assert store.resource_reservations() == ()

        with store.transaction() as uow:
            uow.delete_resource_demand(demand.request_id)
            uow.save_resource_reservation(reservation)

        assert store.resource_demands() == ()
        assert store.resource_reservations() == (reservation,)

        with pytest.raises(RuntimeError, match="abort release intent"):
            with store.transaction() as uow:
                uow.save_resource_release_intent(release)
                raise RuntimeError("abort release intent")

        assert store.resource_release_intents() == ()

        with store.transaction() as uow:
            uow.save_resource_release_intent(release)
            assert uow.get_resource_release_intent(release.intent_id) == release

        assert store.resource_release_intents() == (release,)

        with pytest.raises(RuntimeError, match="abort release finalize"):
            with store.transaction() as uow:
                uow.delete_resource_reservation(reservation.reservation_id)
                uow.delete_resource_release_intent(release.intent_id)
                raise RuntimeError("abort release finalize")

        assert store.resource_reservations() == (reservation,)
        assert store.resource_release_intents() == (release,)

        with store.transaction() as uow:
            uow.delete_resource_reservation(reservation.reservation_id)
            uow.delete_resource_release_intent(release.intent_id)

        assert store.resource_reservations() == ()
        assert store.resource_release_intents() == ()

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

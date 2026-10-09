from __future__ import annotations

from contextlib import contextmanager
from copy import deepcopy
from dataclasses import dataclass, field, fields
from datetime import datetime
from typing import Iterator

from sose.core.events import Command, DomainEvent
from sose.core.runtime import (
    ResourceDefinition,
    ResourceDemand,
    ResourceReleaseIntent,
    ResourceReservation,
    StoreDefinition,
    DurableStoreItem,
    StorePutIntent,
    StoreGetRequest,
    StoreGetResult,
    ContainerDefinition,
    ContainerState,
    ContainerOperationIntent,
    ContainerOperationResult,
    PreemptiveResourceDefinition,
    PreemptiveResourceDemand,
    PreemptiveResourceReservation,
    PreemptiveResourceReleaseIntent,
    ResourcePreemptionResult,
    ScheduledWork,
    SimulationPosition,
)
from sose.domain.entity import Entity
from sose.domain.delivery import DomainDelivery
from sose.composition.model import BoundaryConsumption, BoundaryDelivery, BoundaryMessage
from sose.composition.effects import BusinessEffectApplied
from sose.composition.bindings import CustomerSettlementBinding
from sose.scenarios.model import ScenarioRuntimeState
from sose.jobs.model import SimulationJobState
from sose.sinks.model import SinkCheckpoint, SinkDelivery


@dataclass
class _State:
    entities: dict[tuple[str, str], Entity] = field(default_factory=dict)
    events: list[DomainEvent] = field(default_factory=list)
    commands: dict[str, Command] = field(default_factory=dict)
    scheduled_work: dict[str, ScheduledWork] = field(default_factory=dict)
    simulation_position: SimulationPosition | None = None
    scenario_state: ScenarioRuntimeState | None = None
    resource_definitions: dict[str, ResourceDefinition] = field(default_factory=dict)
    resource_demands: dict[str, ResourceDemand] = field(default_factory=dict)
    resource_reservations: dict[str, ResourceReservation] = field(default_factory=dict)
    resource_release_intents: dict[str, ResourceReleaseIntent] = field(default_factory=dict)
    store_definitions: dict[str, StoreDefinition] = field(default_factory=dict)
    store_items: dict[str, DurableStoreItem] = field(default_factory=dict)
    store_put_intents: dict[str, StorePutIntent] = field(default_factory=dict)
    store_get_requests: dict[str, StoreGetRequest] = field(default_factory=dict)
    store_get_results: dict[str, StoreGetResult] = field(default_factory=dict)
    container_definitions: dict[str, ContainerDefinition] = field(default_factory=dict)
    container_states: dict[str, ContainerState] = field(default_factory=dict)
    container_operation_intents: dict[str, ContainerOperationIntent] = field(default_factory=dict)
    container_operation_results: dict[str, ContainerOperationResult] = field(default_factory=dict)
    preemptive_resource_definitions: dict[str, PreemptiveResourceDefinition] = field(default_factory=dict)
    preemptive_resource_demands: dict[str, PreemptiveResourceDemand] = field(default_factory=dict)
    preemptive_resource_reservations: dict[str, PreemptiveResourceReservation] = field(default_factory=dict)
    preemptive_resource_release_intents: dict[str, PreemptiveResourceReleaseIntent] = field(default_factory=dict)
    resource_preemption_results: dict[str, ResourcePreemptionResult] = field(default_factory=dict)
    job_states: dict[str, SimulationJobState] = field(default_factory=dict)
    sink_deliveries: dict[str, SinkDelivery] = field(default_factory=dict)
    sink_checkpoints: dict[tuple[str, str], SinkCheckpoint] = field(default_factory=dict)
    boundary_messages: dict[str, BoundaryMessage] = field(default_factory=dict)
    boundary_deliveries: dict[str, BoundaryDelivery] = field(default_factory=dict)
    boundary_consumptions: dict[str, BoundaryConsumption] = field(default_factory=dict)
    business_effects: dict[str, BusinessEffectApplied] = field(default_factory=dict)
    customer_settlement_bindings: dict[tuple[str, str], CustomerSettlementBinding] = field(default_factory=dict)
    domain_deliveries: dict[str, DomainDelivery] = field(default_factory=dict)
    committed_tick: int = -1


def fork_state(state: _State) -> _State:
    """Create a transaction-local structural copy of durable state.

    Top-level mutable collections are copied, while existing record objects are
    shared until a UnitOfWork explicitly reads/saves them through methods that
    already deepcopy values. This avoids an O(total object graph) deepcopy at
    transaction entry without weakening rollback isolation.
    """

    values: dict[str, object] = {}
    for field_info in fields(_State):
        value = getattr(state, field_info.name)
        if isinstance(value, dict):
            values[field_info.name] = dict(value)
        elif isinstance(value, list):
            values[field_info.name] = list(value)
        else:
            values[field_info.name] = value
    return _State(**values)


class MemoryUnitOfWork:
    def __init__(self, working: _State, owner: "MemoryPersistence") -> None:
        self._working = working
        self._owner = owner
        self._closed = False
        self._dirty_records: set[tuple[str, object]] = set()

    @property
    def dirty_records(self) -> frozenset[tuple[str, object]]:
        """Internal record identities touched by this unit of work."""
        return frozenset(self._dirty_records)

    def _mark_dirty(self, collection: str, key: object) -> None:
        self._dirty_records.add((collection, key))


    def get_job_state(self, job_id: str) -> SimulationJobState | None:
        value = self._working.job_states.get(job_id)
        return deepcopy(value) if value else None

    def save_job_state(self, state: SimulationJobState) -> None:
        existing = self._working.job_states.get(state.job_id)
        if existing is not None and existing.domain_name != state.domain_name:
            raise ValueError(
                f"job domain cannot change: {state.job_id} "
                f"{existing.domain_name!r} -> {state.domain_name!r}"
            )
        self._working.job_states[state.job_id] = deepcopy(state)
        self._mark_dirty("job_states", state.job_id)


    def get_boundary_message(self, message_id: str) -> BoundaryMessage | None:
        value = self._working.boundary_messages.get(message_id)
        return deepcopy(value) if value else None

    def save_boundary_message(self, message: BoundaryMessage) -> None:
        existing = self._working.boundary_messages.get(message.message_id)
        if existing is not None and existing != message:
            raise ValueError(
                f"boundary message identity conflict: {message.message_id}"
            )
        self._working.boundary_messages[message.message_id] = deepcopy(message)
        self._mark_dirty("boundary_messages", message.message_id)

    def get_boundary_delivery(self, delivery_id: str) -> BoundaryDelivery | None:
        value = self._working.boundary_deliveries.get(delivery_id)
        return deepcopy(value) if value else None

    def boundary_deliveries(self) -> tuple[BoundaryDelivery, ...]:
        return tuple(
            deepcopy(self._working.boundary_deliveries[key])
            for key in sorted(self._working.boundary_deliveries)
        )

    def save_boundary_delivery(self, delivery: BoundaryDelivery) -> None:
        existing = self._working.boundary_deliveries.get(delivery.delivery_id)
        if existing is not None and existing.message_id != delivery.message_id:
            raise ValueError(
                f"boundary delivery identity conflict: {delivery.delivery_id}"
            )
        self._working.boundary_deliveries[delivery.delivery_id] = deepcopy(delivery)
        self._mark_dirty("boundary_deliveries", delivery.delivery_id)

    def get_boundary_consumption(self, delivery_id: str) -> BoundaryConsumption | None:
        value = self._working.boundary_consumptions.get(delivery_id)
        return deepcopy(value) if value else None

    def save_boundary_consumption(self, consumption: BoundaryConsumption) -> None:
        existing = self._working.boundary_consumptions.get(consumption.delivery_id)
        if existing is not None and existing != consumption:
            raise ValueError(
                f"boundary consumption identity conflict: {consumption.delivery_id}"
            )
        self._working.boundary_consumptions[consumption.delivery_id] = deepcopy(
            consumption
        )
        self._mark_dirty("boundary_consumptions", consumption.delivery_id)

    def get_customer_settlement_binding(
        self, kind: str, entity_id: str,
    ) -> CustomerSettlementBinding | None:
        if kind not in {"order", "payment", "journal"}:
            raise ValueError("unknown settlement binding identity kind")
        value = self._working.customer_settlement_bindings.get((kind, entity_id))
        return deepcopy(value) if value is not None else None

    def save_customer_settlement_binding(self, binding: CustomerSettlementBinding) -> None:
        # Three independent lookup keys are committed together in one UoW.
        # Assignment of any order, payment or journal ID is immutable. The
        # public service also takes the shared PostgreSQL boundary lock.
        indices = (
            ("order", binding.order_id),
            ("payment", binding.payment_id),
            ("journal", binding.journal_id),
        )
        for key in indices:
            existing = self._working.customer_settlement_bindings.get(key)
            if existing is not None and existing != binding:
                raise ValueError(f"settlement identity already bound: {key}")
        for key in indices:
            self._working.customer_settlement_bindings[key] = deepcopy(binding)
            self._mark_dirty("customer_settlement_bindings", key)

    def get_business_effect(self, effect_id: str) -> BusinessEffectApplied | None:
        result = self._working.business_effects.get(effect_id)
        return deepcopy(result) if result else None

    def save_business_effect(self, effect: BusinessEffectApplied) -> None:
        previous = self._working.business_effects.get(effect.effect_id)
        if previous is not None and previous != effect:
            raise ValueError(f"business-effect certificate identity conflicts: {effect.effect_id}")
        self._working.business_effects[effect.effect_id] = deepcopy(effect)
        self._mark_dirty("business_effects", effect.effect_id)

    def get_domain_delivery(self, mutation_id: str) -> DomainDelivery | None:
        value = self._working.domain_deliveries.get(mutation_id)
        return deepcopy(value) if value else None

    def save_domain_delivery(self, delivery: DomainDelivery) -> None:
        existing = self._working.domain_deliveries.get(delivery.mutation_id)
        if existing is not None and existing.mutation != delivery.mutation:
            raise ValueError(
                f"domain mutation identity conflict: {delivery.mutation_id}"
            )
        self._working.domain_deliveries[delivery.mutation_id] = deepcopy(delivery)
        self._mark_dirty("domain_deliveries", delivery.mutation_id)

    def delete_domain_delivery(self, mutation_id: str) -> None:
        self._working.domain_deliveries.pop(mutation_id, None)
        self._mark_dirty("domain_deliveries", mutation_id)

    def get_sink_delivery(self, delivery_id: str) -> SinkDelivery | None:
        value = self._working.sink_deliveries.get(delivery_id)
        return deepcopy(value) if value else None

    def save_sink_delivery(self, delivery: SinkDelivery) -> None:
        self._working.sink_deliveries[delivery.delivery_id] = deepcopy(delivery)
        self._mark_dirty("sink_deliveries", delivery.delivery_id)

    def delete_sink_delivery(self, delivery_id: str) -> None:
        self._working.sink_deliveries.pop(delivery_id, None)
        self._mark_dirty("sink_deliveries", delivery_id)

    def get_sink_checkpoint(
        self,
        job_id: str,
        sink_name: str,
    ) -> SinkCheckpoint | None:
        value = self._working.sink_checkpoints.get((job_id, sink_name))
        return deepcopy(value) if value else None

    def save_sink_checkpoint(self, checkpoint: SinkCheckpoint) -> None:
        key = (checkpoint.job_id, checkpoint.sink_name)
        self._working.sink_checkpoints[key] = deepcopy(checkpoint)
        self._mark_dirty("sink_checkpoints", key)

    def get_entity(self, entity_type: str, entity_id: str) -> Entity | None:
        value = self._working.entities.get((entity_type, entity_id))
        return deepcopy(value) if value else None

    def save_entity(self, entity: Entity) -> None:
        key = (entity.entity_type, entity.id)
        self._working.entities[key] = deepcopy(entity)
        self._mark_dirty("entities", key)

    def get_event(self, event_id: str) -> DomainEvent | None:
        matches = [event for event in self._working.events if event.event_id == event_id]
        if any(event != matches[0] for event in matches[1:]):
            raise ValueError(f"conflicting durable event identity: {event_id}")
        return deepcopy(matches[0]) if matches else None

    def append_event(self, event: DomainEvent) -> None:
        self._working.events.append(deepcopy(event))
        self._mark_dirty("events", len(self._working.events) - 1)

    def get_command(self, command_id: str) -> Command | None:
        value = self._working.commands.get(command_id)
        return deepcopy(value) if value else None

    def save_command(self, command: Command) -> None:
        self._working.commands[command.command_id] = deepcopy(command)
        self._mark_dirty("commands", command.command_id)

    def delete_command(self, command_id: str) -> None:
        self._working.commands.pop(command_id, None)
        self._mark_dirty("commands", command_id)

    def get_scheduled_work(self, work_id: str) -> ScheduledWork | None:
        value = self._working.scheduled_work.get(work_id)
        return deepcopy(value) if value else None

    def save_scheduled_work(self, work: ScheduledWork) -> None:
        if work.command_id not in self._working.commands:
            raise KeyError(f"unknown scheduled command: {work.command_id}")
        self._working.scheduled_work[work.work_id] = deepcopy(work)
        self._mark_dirty("scheduled_work", work.work_id)

    def delete_scheduled_work(self, work_id: str) -> None:
        self._working.scheduled_work.pop(work_id, None)
        self._mark_dirty("scheduled_work", work_id)

    def set_simulation_position(self, position: SimulationPosition) -> None:
        self._working.simulation_position = deepcopy(position)
        self._mark_dirty("simulation_position", "__scalar__")

    def set_scenario_state(self, state: ScenarioRuntimeState) -> None:
        self._working.scenario_state = deepcopy(state)
        self._mark_dirty("scenario_state", "__scalar__")

    def get_resource_demand(self, request_id: str) -> ResourceDemand | None:
        value = self._working.resource_demands.get(request_id)
        return deepcopy(value) if value else None

    def save_resource_definition(self, definition: ResourceDefinition) -> None:
        existing = self._working.resource_definitions.get(definition.name)
        if existing is not None and existing != definition:
            raise ValueError(f"resource definition already exists: {definition.name}")
        self._working.resource_definitions[definition.name] = deepcopy(definition)
        self._mark_dirty("resource_definitions", definition.name)

    def save_resource_demand(self, demand: ResourceDemand) -> None:
        if demand.resource_name not in self._working.resource_definitions:
            raise KeyError(f"unknown resource definition: {demand.resource_name}")
        if any(
            reservation.request_id == demand.request_id
            for reservation in self._working.resource_reservations.values()
        ):
            raise ValueError(f"resource request already reserved: {demand.request_id}")
        existing = self._working.resource_demands.get(demand.request_id)
        if existing is not None and existing != demand:
            raise ValueError(f"resource demand already exists: {demand.request_id}")
        self._working.resource_demands[demand.request_id] = deepcopy(demand)
        self._mark_dirty("resource_demands", demand.request_id)

    def delete_resource_demand(self, request_id: str) -> None:
        self._working.resource_demands.pop(request_id, None)
        self._mark_dirty("resource_demands", request_id)

    def get_resource_reservation(self, reservation_id: str) -> ResourceReservation | None:
        value = self._working.resource_reservations.get(reservation_id)
        return deepcopy(value) if value else None

    def save_resource_reservation(self, reservation: ResourceReservation) -> None:
        if reservation.resource_name not in self._working.resource_definitions:
            raise KeyError(f"unknown resource definition: {reservation.resource_name}")
        if reservation.request_id in self._working.resource_demands:
            raise ValueError(f"resource request is still pending: {reservation.request_id}")
        if any(
            current.request_id == reservation.request_id
            and current.reservation_id != reservation.reservation_id
            for current in self._working.resource_reservations.values()
        ):
            raise ValueError(f"resource request already reserved: {reservation.request_id}")
        self._working.resource_reservations[reservation.reservation_id] = deepcopy(reservation)
        self._mark_dirty("resource_reservations", reservation.reservation_id)

    def delete_resource_reservation(self, reservation_id: str) -> None:
        self._working.resource_reservations.pop(reservation_id, None)
        self._mark_dirty("resource_reservations", reservation_id)

    def get_resource_release_intent(self, intent_id: str) -> ResourceReleaseIntent | None:
        value = self._working.resource_release_intents.get(intent_id)
        return deepcopy(value) if value else None

    def save_resource_release_intent(self, intent: ResourceReleaseIntent) -> None:
        if intent.reservation_id not in self._working.resource_reservations:
            raise KeyError(f"unknown resource reservation: {intent.reservation_id}")
        existing = self._working.resource_release_intents.get(intent.intent_id)
        if existing is not None and existing != intent:
            raise ValueError(f"resource release intent already exists: {intent.intent_id}")
        self._working.resource_release_intents[intent.intent_id] = deepcopy(intent)
        self._mark_dirty("resource_release_intents", intent.intent_id)

    def delete_resource_release_intent(self, intent_id: str) -> None:
        self._working.resource_release_intents.pop(intent_id, None)
        self._mark_dirty("resource_release_intents", intent_id)


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

    def get_container_operation_intent(
        self, request_id: str
    ) -> ContainerOperationIntent | None:
        value = self._working.container_operation_intents.get(request_id)
        return deepcopy(value) if value else None

    def get_container_operation_result(
        self, request_id: str
    ) -> ContainerOperationResult | None:
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

    def get_preemptive_resource_demand(
        self, request_id: str
    ) -> PreemptiveResourceDemand | None:
        value = self._working.preemptive_resource_demands.get(request_id)
        return deepcopy(value) if value else None

    def get_preemptive_resource_reservation(
        self, reservation_id: str
    ) -> PreemptiveResourceReservation | None:
        value = self._working.preemptive_resource_reservations.get(reservation_id)
        return deepcopy(value) if value else None

    def get_preemptive_resource_release_intent(
        self, intent_id: str
    ) -> PreemptiveResourceReleaseIntent | None:
        value = self._working.preemptive_resource_release_intents.get(intent_id)
        return deepcopy(value) if value else None

    def get_resource_preemption_result(
        self, result_id: str
    ) -> ResourcePreemptionResult | None:
        value = self._working.resource_preemption_results.get(result_id)
        return deepcopy(value) if value else None

    def save_preemptive_resource_definition(
        self, definition: PreemptiveResourceDefinition
    ) -> None:
        existing = self._working.preemptive_resource_definitions.get(definition.name)
        if existing is not None and existing != definition:
            raise ValueError(f"preemptive resource definition already exists: {definition.name}")
        self._working.preemptive_resource_definitions[definition.name] = deepcopy(definition)
        self._mark_dirty("preemptive_resource_definitions", definition.name)

    def save_preemptive_resource_demand(self, demand: PreemptiveResourceDemand) -> None:
        if demand.resource_name not in self._working.preemptive_resource_definitions:
            raise KeyError(f"unknown preemptive resource definition: {demand.resource_name}")
        if any(
            reservation.request_id == demand.request_id
            for reservation in self._working.preemptive_resource_reservations.values()
        ):
            raise ValueError(f"preemptive resource request already reserved: {demand.request_id}")
        existing = self._working.preemptive_resource_demands.get(demand.request_id)
        if existing is not None and existing != demand:
            raise ValueError(f"preemptive resource demand already exists: {demand.request_id}")
        self._working.preemptive_resource_demands[demand.request_id] = deepcopy(demand)
        self._mark_dirty("preemptive_resource_demands", demand.request_id)

    def delete_preemptive_resource_demand(self, request_id: str) -> None:
        self._working.preemptive_resource_demands.pop(request_id, None)
        self._mark_dirty("preemptive_resource_demands", request_id)

    def save_preemptive_resource_reservation(
        self, reservation: PreemptiveResourceReservation
    ) -> None:
        if reservation.resource_name not in self._working.preemptive_resource_definitions:
            raise KeyError(
                f"unknown preemptive resource definition: {reservation.resource_name}"
            )
        if reservation.request_id in self._working.preemptive_resource_demands:
            raise ValueError(
                f"preemptive resource request is still pending: {reservation.request_id}"
            )
        if any(
            current.request_id == reservation.request_id
            and current.reservation_id != reservation.reservation_id
            for current in self._working.preemptive_resource_reservations.values()
        ):
            raise ValueError(
                f"preemptive resource request already reserved: {reservation.request_id}"
            )
        self._working.preemptive_resource_reservations[reservation.reservation_id] = deepcopy(
            reservation
        )
        self._mark_dirty(
            "preemptive_resource_reservations",
            reservation.reservation_id,
        )

    def delete_preemptive_resource_reservation(self, reservation_id: str) -> None:
        self._working.preemptive_resource_reservations.pop(reservation_id, None)
        self._mark_dirty("preemptive_resource_reservations", reservation_id)

    def save_preemptive_resource_release_intent(
        self, intent: PreemptiveResourceReleaseIntent
    ) -> None:
        if intent.reservation_id not in self._working.preemptive_resource_reservations:
            raise KeyError(
                f"unknown preemptive resource reservation: {intent.reservation_id}"
            )
        existing = self._working.preemptive_resource_release_intents.get(intent.intent_id)
        if existing is not None and existing != intent:
            raise ValueError(
                f"preemptive resource release intent already exists: {intent.intent_id}"
            )
        self._working.preemptive_resource_release_intents[intent.intent_id] = deepcopy(intent)
        self._mark_dirty(
            "preemptive_resource_release_intents",
            intent.intent_id,
        )

    def delete_preemptive_resource_release_intent(self, intent_id: str) -> None:
        self._working.preemptive_resource_release_intents.pop(intent_id, None)
        self._mark_dirty("preemptive_resource_release_intents", intent_id)

    def save_resource_preemption_result(self, result: ResourcePreemptionResult) -> None:
        successor = self._working.preemptive_resource_reservations.get(
            result.successor_reservation_id
        )
        if successor is None:
            raise KeyError(
                f"unknown successor preemptive reservation: {result.successor_reservation_id}"
            )
        if successor.request_id != result.preempting_request_id:
            raise ValueError(
                f"preemption result successor does not match request: {result.result_id}"
            )
        if result.displaced_reservation_id in self._working.preemptive_resource_reservations:
            raise ValueError(
                f"displaced reservation is still active: {result.displaced_reservation_id}"
            )
        existing = self._working.resource_preemption_results.get(result.result_id)
        if existing is not None and existing != result:
            raise ValueError(f"resource preemption result already exists: {result.result_id}")
        self._working.resource_preemption_results[result.result_id] = deepcopy(result)
        self._mark_dirty("resource_preemption_results", result.result_id)

    def set_committed_tick(self, tick: int) -> None:
        self._working.committed_tick = tick
        self._mark_dirty("committed_tick", "__scalar__")

    def commit(self) -> None:
        store_overlap = (
            self._working.store_get_requests.keys()
            & self._working.store_get_results.keys()
        )
        if store_overlap:
            request_id = sorted(store_overlap)[0]
            raise ValueError(
                f"store get identity cannot be pending and completed: {request_id}"
            )
        container_overlap = (
            self._working.container_operation_intents.keys()
            & self._working.container_operation_results.keys()
        )
        if container_overlap:
            request_id = sorted(container_overlap)[0]
            raise ValueError(
                "container operation identity cannot be pending and completed: "
                f"{request_id}"
            )
        self._owner._state = self._working
        self._closed = True

    def rollback(self) -> None:
        self._closed = True


class MemoryPersistence:
    def __init__(self) -> None:
        self._state = _State()

    @contextmanager
    def transaction(self) -> Iterator[MemoryUnitOfWork]:
        uow = MemoryUnitOfWork(fork_state(self._state), self)
        try:
            yield uow
            if not uow._closed:
                uow.commit()
        except Exception:
            uow.rollback()
            raise


    def job_state(self, job_id: str) -> SimulationJobState | None:
        value = self._state.job_states.get(job_id)
        return deepcopy(value) if value else None

    def job_states(self) -> tuple[SimulationJobState, ...]:
        return tuple(
            deepcopy(self._state.job_states[job_id])
            for job_id in sorted(self._state.job_states)
        )

    def business_effect(self, effect_id: str) -> BusinessEffectApplied | None:
        value = self._state.business_effects.get(effect_id)
        return deepcopy(value) if value else None

    def business_effects(self) -> tuple[BusinessEffectApplied, ...]:
        return tuple(
            deepcopy(self._state.business_effects[key])
            for key in sorted(self._state.business_effects)
        )

    def domain_delivery(self, mutation_id: str) -> DomainDelivery | None:
        value = self._state.domain_deliveries.get(mutation_id)
        return deepcopy(value) if value else None

    def domain_deliveries(self) -> tuple[DomainDelivery, ...]:
        return tuple(
            deepcopy(self._state.domain_deliveries[key])
            for key in sorted(self._state.domain_deliveries)
        )

    def sink_delivery(self, delivery_id: str) -> SinkDelivery | None:
        value = self._state.sink_deliveries.get(delivery_id)
        return deepcopy(value) if value else None

    def sink_deliveries(
        self,
        *,
        job_id: str | None = None,
        sink_name: str | None = None,
    ) -> tuple[SinkDelivery, ...]:
        values = self._state.sink_deliveries.values()
        return tuple(
            deepcopy(delivery)
            for delivery in sorted(values, key=lambda item: item.delivery_id)
            if (job_id is None or delivery.batch.job_id == job_id)
            and (sink_name is None or delivery.sink_name == sink_name)
        )

    def sink_checkpoint(
        self,
        job_id: str,
        sink_name: str,
    ) -> SinkCheckpoint | None:
        value = self._state.sink_checkpoints.get((job_id, sink_name))
        return deepcopy(value) if value else None

    def sink_checkpoints(
        self,
        *,
        job_id: str | None = None,
    ) -> tuple[SinkCheckpoint, ...]:
        values = self._state.sink_checkpoints.values()
        return tuple(
            deepcopy(checkpoint)
            for checkpoint in sorted(
                values,
                key=lambda item: (item.job_id, item.sink_name),
            )
            if job_id is None or checkpoint.job_id == job_id
        )

    def committed_tick(self) -> int:
        return self._state.committed_tick

    def events(self) -> tuple[DomainEvent, ...]:
        return tuple(deepcopy(self._state.events))

    def entity(self, entity_type: str, entity_id: str) -> Entity | None:
        value = self._state.entities.get((entity_type, entity_id))
        return deepcopy(value) if value else None

    def entities(self) -> tuple[Entity, ...]:
        return tuple(deepcopy(tuple(self._state.entities.values())))

    def command(self, command_id: str) -> Command | None:
        value = self._state.commands.get(command_id)
        return deepcopy(value) if value else None

    def scheduled_work(self) -> tuple[ScheduledWork, ...]:
        return tuple(sorted(deepcopy(tuple(self._state.scheduled_work.values()))))

    def due_scheduled_work(self, at: datetime) -> tuple[ScheduledWork, ...]:
        return tuple(work for work in self.scheduled_work() if work.due_at <= at)

    def simulation_position(self) -> SimulationPosition | None:
        return deepcopy(self._state.simulation_position)

    def scenario_state(self) -> ScenarioRuntimeState | None:
        return deepcopy(self._state.scenario_state)

    def resource_definitions(self) -> tuple[ResourceDefinition, ...]:
        return tuple(
            deepcopy(self._state.resource_definitions[name])
            for name in sorted(self._state.resource_definitions)
        )

    def resource_demands(self) -> tuple[ResourceDemand, ...]:
        return tuple(sorted(deepcopy(tuple(self._state.resource_demands.values()))))

    def resource_reservations(self) -> tuple[ResourceReservation, ...]:
        return tuple(
            deepcopy(self._state.resource_reservations[key])
            for key in sorted(self._state.resource_reservations)
        )

    def resource_release_intents(self) -> tuple[ResourceReleaseIntent, ...]:
        return tuple(
            deepcopy(self._state.resource_release_intents[key])
            for key in sorted(self._state.resource_release_intents)
        )


    def store_definitions(self) -> tuple[StoreDefinition, ...]:
        return tuple(
            deepcopy(self._state.store_definitions[name])
            for name in sorted(self._state.store_definitions)
        )

    def store_items(self) -> tuple[DurableStoreItem, ...]:
        return tuple(
            sorted(
                deepcopy(tuple(self._state.store_items.values())),
                key=lambda item: (item.store_name, item.sequence, item.item_id),
            )
        )

    def store_put_intents(self) -> tuple[StorePutIntent, ...]:
        return tuple(
            sorted(
                deepcopy(tuple(self._state.store_put_intents.values())),
                key=lambda intent: (intent.sequence, intent.item_id),
            )
        )

    def store_get_requests(self) -> tuple[StoreGetRequest, ...]:
        return tuple(
            sorted(
                deepcopy(tuple(self._state.store_get_requests.values())),
                key=lambda request: (request.sequence, request.request_id),
            )
        )


    def store_get_results(self) -> tuple[StoreGetResult, ...]:
        return tuple(
            sorted(
                deepcopy(tuple(self._state.store_get_results.values())),
                key=lambda result: (result.sequence, result.request_id),
            )
        )



    def container_definitions(self) -> tuple[ContainerDefinition, ...]:
        return tuple(
            deepcopy(self._state.container_definitions[name])
            for name in sorted(self._state.container_definitions)
        )

    def container_states(self) -> tuple[ContainerState, ...]:
        return tuple(
            deepcopy(self._state.container_states[name])
            for name in sorted(self._state.container_states)
        )

    def container_operation_intents(self) -> tuple[ContainerOperationIntent, ...]:
        return tuple(
            sorted(
                deepcopy(tuple(self._state.container_operation_intents.values())),
                key=lambda intent: (intent.sequence, intent.request_id),
            )
        )

    def container_operation_results(self) -> tuple[ContainerOperationResult, ...]:
        return tuple(
            sorted(
                deepcopy(tuple(self._state.container_operation_results.values())),
                key=lambda result: (result.sequence, result.request_id),
            )
        )

    def preemptive_resource_definitions(self) -> tuple[PreemptiveResourceDefinition, ...]:
        return tuple(
            deepcopy(self._state.preemptive_resource_definitions[name])
            for name in sorted(self._state.preemptive_resource_definitions)
        )

    def preemptive_resource_demands(self) -> tuple[PreemptiveResourceDemand, ...]:
        return tuple(
            sorted(deepcopy(tuple(self._state.preemptive_resource_demands.values())))
        )

    def preemptive_resource_reservations(
        self,
    ) -> tuple[PreemptiveResourceReservation, ...]:
        return tuple(
            sorted(
                deepcopy(tuple(self._state.preemptive_resource_reservations.values())),
                key=lambda reservation: (
                    reservation.resource_name,
                    reservation.sequence,
                    reservation.reservation_id,
                ),
            )
        )

    def preemptive_resource_release_intents(
        self,
    ) -> tuple[PreemptiveResourceReleaseIntent, ...]:
        return tuple(
            deepcopy(self._state.preemptive_resource_release_intents[key])
            for key in sorted(self._state.preemptive_resource_release_intents)
        )

    def resource_preemption_results(self) -> tuple[ResourcePreemptionResult, ...]:
        return tuple(
            sorted(
                deepcopy(tuple(self._state.resource_preemption_results.values())),
                key=lambda result: (result.sequence, result.result_id),
            )
        )


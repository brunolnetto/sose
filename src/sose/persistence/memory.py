from __future__ import annotations

from contextlib import contextmanager
from copy import deepcopy
from dataclasses import dataclass, field
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
    PreemptiveResourceDefinition,
    PreemptiveResourceDemand,
    PreemptiveResourceReservation,
    PreemptiveResourceReleaseIntent,
    ResourcePreemptionResult,
    ScheduledWork,
    SimulationPosition,
)
from sose.domain.entity import Entity
from sose.scenarios.model import ScenarioRuntimeState


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
    preemptive_resource_definitions: dict[str, PreemptiveResourceDefinition] = field(default_factory=dict)
    preemptive_resource_demands: dict[str, PreemptiveResourceDemand] = field(default_factory=dict)
    preemptive_resource_reservations: dict[str, PreemptiveResourceReservation] = field(default_factory=dict)
    preemptive_resource_release_intents: dict[str, PreemptiveResourceReleaseIntent] = field(default_factory=dict)
    resource_preemption_results: dict[str, ResourcePreemptionResult] = field(default_factory=dict)
    committed_tick: int = -1


class MemoryUnitOfWork:
    def __init__(self, working: _State, owner: "MemoryPersistence") -> None:
        self._working = working
        self._owner = owner
        self._closed = False

    def get_entity(self, entity_type: str, entity_id: str) -> Entity | None:
        value = self._working.entities.get((entity_type, entity_id))
        return deepcopy(value) if value else None

    def save_entity(self, entity: Entity) -> None:
        self._working.entities[(entity.entity_type, entity.id)] = deepcopy(entity)

    def append_event(self, event: DomainEvent) -> None:
        self._working.events.append(event)

    def get_command(self, command_id: str) -> Command | None:
        value = self._working.commands.get(command_id)
        return deepcopy(value) if value else None

    def save_command(self, command: Command) -> None:
        self._working.commands[command.command_id] = deepcopy(command)

    def delete_command(self, command_id: str) -> None:
        self._working.commands.pop(command_id, None)

    def get_scheduled_work(self, work_id: str) -> ScheduledWork | None:
        value = self._working.scheduled_work.get(work_id)
        return deepcopy(value) if value else None

    def save_scheduled_work(self, work: ScheduledWork) -> None:
        if work.command_id not in self._working.commands:
            raise KeyError(f"unknown scheduled command: {work.command_id}")
        self._working.scheduled_work[work.work_id] = deepcopy(work)

    def delete_scheduled_work(self, work_id: str) -> None:
        self._working.scheduled_work.pop(work_id, None)

    def set_simulation_position(self, position: SimulationPosition) -> None:
        self._working.simulation_position = deepcopy(position)

    def set_scenario_state(self, state: ScenarioRuntimeState) -> None:
        self._working.scenario_state = deepcopy(state)

    def get_resource_demand(self, request_id: str) -> ResourceDemand | None:
        value = self._working.resource_demands.get(request_id)
        return deepcopy(value) if value else None

    def save_resource_definition(self, definition: ResourceDefinition) -> None:
        existing = self._working.resource_definitions.get(definition.name)
        if existing is not None and existing != definition:
            raise ValueError(f"resource definition already exists: {definition.name}")
        self._working.resource_definitions[definition.name] = deepcopy(definition)

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

    def delete_resource_demand(self, request_id: str) -> None:
        self._working.resource_demands.pop(request_id, None)

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

    def delete_resource_reservation(self, reservation_id: str) -> None:
        self._working.resource_reservations.pop(reservation_id, None)

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

    def delete_resource_release_intent(self, intent_id: str) -> None:
        self._working.resource_release_intents.pop(intent_id, None)


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

    def save_store_item(self, item: DurableStoreItem) -> None:
        if item.store_name not in self._working.store_definitions:
            raise KeyError(f"unknown store definition: {item.store_name}")
        if item.item_id in self._working.store_put_intents:
            raise ValueError(f"store item is still pending put: {item.item_id}")
        existing = self._working.store_items.get(item.item_id)
        if existing is not None and existing != item:
            raise ValueError(f"store item already exists: {item.item_id}")
        self._working.store_items[item.item_id] = deepcopy(item)

    def delete_store_item(self, item_id: str) -> None:
        self._working.store_items.pop(item_id, None)

    def save_store_put_intent(self, intent: StorePutIntent) -> None:
        if intent.store_name not in self._working.store_definitions:
            raise KeyError(f"unknown store definition: {intent.store_name}")
        if intent.item_id in self._working.store_items:
            raise ValueError(f"store item already accepted: {intent.item_id}")
        existing = self._working.store_put_intents.get(intent.item_id)
        if existing is not None and existing != intent:
            raise ValueError(f"store put intent already exists: {intent.item_id}")
        self._working.store_put_intents[intent.item_id] = deepcopy(intent)

    def delete_store_put_intent(self, item_id: str) -> None:
        self._working.store_put_intents.pop(item_id, None)

    def save_store_get_request(self, request: StoreGetRequest) -> None:
        if request.store_name not in self._working.store_definitions:
            raise KeyError(f"unknown store definition: {request.store_name}")
        if request.request_id in self._working.store_get_results:
            raise ValueError(f"store get request already completed: {request.request_id}")
        existing = self._working.store_get_requests.get(request.request_id)
        if existing is not None and existing != request:
            raise ValueError(f"store get request already exists: {request.request_id}")
        self._working.store_get_requests[request.request_id] = deepcopy(request)

    def delete_store_get_request(self, request_id: str) -> None:
        self._working.store_get_requests.pop(request_id, None)

    def save_store_get_result(self, result: StoreGetResult) -> None:
        if result.store_name not in self._working.store_definitions:
            raise KeyError(f"unknown store definition: {result.store_name}")
        request = self._working.store_get_requests.get(result.request_id)
        if request is None:
            raise KeyError(f"unknown store get request: {result.request_id}")
        if request.store_name != result.store_name:
            raise ValueError(f"store get result targets wrong store: {result.request_id}")
        existing = self._working.store_get_results.get(result.request_id)
        if existing is not None and existing != result:
            raise ValueError(f"store get result already exists: {result.request_id}")
        self._working.store_get_results[result.request_id] = deepcopy(result)


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

    def delete_preemptive_resource_demand(self, request_id: str) -> None:
        self._working.preemptive_resource_demands.pop(request_id, None)

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
        self._working.preemptive_resource_reservations[reservation.reservation_id] = deepcopy(
            reservation
        )

    def delete_preemptive_resource_reservation(self, reservation_id: str) -> None:
        self._working.preemptive_resource_reservations.pop(reservation_id, None)

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

    def delete_preemptive_resource_release_intent(self, intent_id: str) -> None:
        self._working.preemptive_resource_release_intents.pop(intent_id, None)

    def save_resource_preemption_result(self, result: ResourcePreemptionResult) -> None:
        existing = self._working.resource_preemption_results.get(result.result_id)
        if existing is not None and existing != result:
            raise ValueError(f"resource preemption result already exists: {result.result_id}")
        self._working.resource_preemption_results[result.result_id] = deepcopy(result)

    def set_committed_tick(self, tick: int) -> None:
        self._working.committed_tick = tick

    def commit(self) -> None:
        self._owner._state = self._working
        self._closed = True

    def rollback(self) -> None:
        self._closed = True


class MemoryPersistence:
    def __init__(self) -> None:
        self._state = _State()

    @contextmanager
    def transaction(self) -> Iterator[MemoryUnitOfWork]:
        uow = MemoryUnitOfWork(deepcopy(self._state), self)
        try:
            yield uow
            if not uow._closed:
                uow.commit()
        except Exception:
            uow.rollback()
            raise

    def committed_tick(self) -> int:
        return self._state.committed_tick

    def events(self) -> tuple[DomainEvent, ...]:
        return tuple(self._state.events)

    def entity(self, entity_type: str, entity_id: str) -> Entity | None:
        value = self._state.entities.get((entity_type, entity_id))
        return deepcopy(value) if value else None

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


"""Committed-state read methods of the in-memory reference persistence.

Split from ``memory.py`` (``MemoryPersistence``).
"""

from __future__ import annotations

from collections.abc import Iterable
from copy import deepcopy
from datetime import datetime
from typing import TYPE_CHECKING

from sose.composition.effects import BusinessEffectApplied
from sose.core.events import Command, DomainEvent
from sose.core.runtime import (
    ContainerDefinition,
    ContainerOperationIntent,
    ContainerOperationResult,
    ContainerState,
    DurableStoreItem,
    PreemptiveResourceDefinition,
    PreemptiveResourceDemand,
    PreemptiveResourceReleaseIntent,
    PreemptiveResourceReservation,
    ResourceDefinition,
    ResourceDemand,
    ResourcePreemptionResult,
    ResourceReleaseIntent,
    ResourceReservation,
    ScheduledWork,
    SimulationPosition,
    StoreDefinition,
    StoreGetRequest,
    StoreGetResult,
    StorePutIntent,
)
from sose.domain.delivery import DomainDelivery
from sose.domain.entity import Entity
from sose.jobs.model import SimulationJobState
from sose.scenarios.model import ScenarioRuntimeState
from sose.sinks.model import SinkCheckpoint, SinkDelivery

if TYPE_CHECKING:
    from .memory import _State


class MemoryCommittedReadsMixin:
    """Detached reads of the committed in-memory state."""

    _state: _State

    def job_state(self, job_id: str) -> SimulationJobState | None:
        value = self._state.job_states.get(job_id)
        return deepcopy(value) if value else None

    def job_states(self) -> tuple[SimulationJobState, ...]:
        return tuple(
            deepcopy(self._state.job_states[job_id]) for job_id in sorted(self._state.job_states)
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

    def committed_tick(self, *, owner_id: str) -> int:
        if not isinstance(owner_id, str) or not owner_id.strip():
            raise ValueError("owner_id must not be empty")
        return self._state.committed_ticks.get(owner_id, -1)

    def count_events(self, *, owner_id: str) -> int:
        if not isinstance(owner_id, str) or not owner_id.strip():
            raise ValueError("owner_id must not be empty")
        return sum(event.owner_id == owner_id for event in self._state.events)

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

    def missing_command_ids(self, command_ids: Iterable[str]) -> frozenset[str]:
        """Return absent command IDs without copying command objects."""
        requested = frozenset(command_ids)
        return requested.difference(self._state.commands)

    def scheduled_work(self) -> tuple[ScheduledWork, ...]:
        return tuple(sorted(self._state.scheduled_work.values()))

    def due_scheduled_work(self, at: datetime) -> tuple[ScheduledWork, ...]:
        return tuple(work for work in self.scheduled_work() if work.due_at <= at)

    def simulation_position(self, *, owner_id: str) -> SimulationPosition | None:
        if not isinstance(owner_id, str) or not owner_id.strip():
            raise ValueError("owner_id must not be empty")
        return deepcopy(self._state.simulation_positions.get(owner_id))

    def scenario_state(self, *, owner_id: str) -> ScenarioRuntimeState | None:
        if not isinstance(owner_id, str) or not owner_id.strip():
            raise ValueError("owner_id must not be empty")
        return deepcopy(self._state.scenario_states.get(owner_id))

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
        return tuple(sorted(deepcopy(tuple(self._state.preemptive_resource_demands.values()))))

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

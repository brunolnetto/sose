"""Entity, event, command, scheduled-work and per-owner cursor methods of the in-memory unit of work.

Split from ``memory.py`` (``MemoryUnitOfWork``).
"""

from __future__ import annotations

from copy import deepcopy
from typing import TYPE_CHECKING

from sose.core.events import Command, DomainEvent
from sose.core.runtime import ScheduledWork, SimulationPosition
from sose.domain.entity import Entity
from sose.scenarios.model import ScenarioRuntimeState

if TYPE_CHECKING:
    from .memory import _State


class CoreRecordsUnitOfWorkMixin:
    """Staged reads/writes of entities, events, commands, scheduled work and owner cursors."""

    _working: _State

    def get_entity(self, entity_type: str, entity_id: str) -> Entity | None:
        value = self._working.entities.get((entity_type, entity_id))
        return deepcopy(value) if value else None

    def entities(self) -> tuple[Entity, ...]:
        """Inspect all entities including orphan observations in this snapshot."""
        return tuple(
            deepcopy(self._working.entities[key]) for key in sorted(self._working.entities)
        )

    def save_entity(self, entity: Entity) -> None:
        key = (entity.entity_type, entity.id)
        self._working.entities[key] = deepcopy(entity)
        self._mark_dirty("entities", key)

    def events(self) -> tuple[DomainEvent, ...]:
        """Return the entire event DAG from THIS UoW snapshot, including staged events."""
        return tuple(deepcopy(self._working.events))

    def get_event(self, event_id: str) -> DomainEvent | None:
        matches = [event for event in self._working.events if event.event_id == event_id]
        if any(event != matches[0] for event in matches[1:]):
            raise ValueError(f"conflicting durable event identity: {event_id}")
        return deepcopy(matches[0]) if matches else None

    def append_event(self, event: DomainEvent) -> None:
        if not isinstance(event.owner_id, str) or not event.owner_id.strip():
            raise ValueError("durable domain event requires operational owner_id")
        self._working.events.append(deepcopy(event))
        self._mark_dirty("events", len(self._working.events) - 1)

    def get_command(self, command_id: str) -> Command | None:
        value = self._working.commands.get(command_id)
        return deepcopy(value) if value else None

    def commands(self) -> tuple[Command, ...]:
        """Enumerate pending Commands in one authoritative UoW snapshot.

        Required to audit extra executable intents not linked to an expected
        boundary message. Preserve complete values and deterministic order.
        """
        return tuple(
            deepcopy(self._working.commands[key]) for key in sorted(self._working.commands)
        )

    def save_command(self, command: Command) -> None:
        self._working.commands[command.command_id] = deepcopy(command)
        self._mark_dirty("commands", command.command_id)

    def delete_command(self, command_id: str) -> None:
        self._working.commands.pop(command_id, None)
        self._mark_dirty("commands", command_id)

    def scheduled_work(self) -> tuple[ScheduledWork, ...]:
        return tuple(sorted(deepcopy(tuple(self._working.scheduled_work.values()))))

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

    def get_simulation_position(self, *, owner_id: str) -> SimulationPosition | None:
        if not isinstance(owner_id, str) or not owner_id.strip():
            raise ValueError("owner_id must not be empty")
        return deepcopy(self._working.simulation_positions.get(owner_id))

    def set_simulation_position(self, position: SimulationPosition, *, owner_id: str) -> None:
        if not isinstance(owner_id, str) or not owner_id.strip():
            raise ValueError("owner_id must not be empty")
        self._working.simulation_positions[owner_id] = deepcopy(position)
        self._mark_dirty("simulation_positions", owner_id)

    def get_scenario_state(self, *, owner_id: str) -> ScenarioRuntimeState | None:
        if not isinstance(owner_id, str) or not owner_id.strip():
            raise ValueError("owner_id must not be empty")
        return deepcopy(self._working.scenario_states.get(owner_id))

    def set_scenario_state(self, state: ScenarioRuntimeState, *, owner_id: str) -> None:
        if not isinstance(owner_id, str) or not owner_id.strip():
            raise ValueError("owner_id must not be empty")
        self._working.scenario_states[owner_id] = deepcopy(state)
        self._mark_dirty("scenario_states", owner_id)

    def set_committed_tick(self, tick: int, *, owner_id: str) -> None:
        if not isinstance(owner_id, str) or not owner_id.strip():
            raise ValueError("owner_id must not be empty")
        self._working.committed_ticks[owner_id] = tick
        self._mark_dirty("committed_ticks", owner_id)

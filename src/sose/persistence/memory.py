from __future__ import annotations

from contextlib import contextmanager
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import datetime
from typing import Iterator

from sose.core.events import Command, DomainEvent
from sose.core.runtime import ScheduledWork, SimulationPosition
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

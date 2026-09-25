from __future__ import annotations

from contextlib import contextmanager
from copy import deepcopy
from dataclasses import dataclass, field
from typing import Iterator

from sose.core.events import DomainEvent
from sose.domain.entity import Entity


@dataclass
class _State:
    entities: dict[tuple[str, str], Entity] = field(default_factory=dict)
    events: list[DomainEvent] = field(default_factory=list)
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

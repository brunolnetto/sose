from __future__ import annotations

import heapq
from dataclasses import dataclass, field
from datetime import datetime

from .events import Command


@dataclass(order=True, slots=True)
class _Scheduled:
    due_at: datetime
    priority: int
    sequence: int
    command: Command = field(compare=False)


class Scheduler:
    def __init__(self) -> None:
        self._heap: list[_Scheduled] = []
        self._sequence = 0

    def schedule(self, command: Command, *, priority: int = 100) -> None:
        self._sequence += 1
        heapq.heappush(
            self._heap,
            _Scheduled(command.due_at, priority, self._sequence, command),
        )

    def due(self, at: datetime) -> list[Command]:
        ready: list[Command] = []
        while self._heap and self._heap[0].due_at <= at:
            ready.append(heapq.heappop(self._heap).command)
        return ready

    def __len__(self) -> int:
        return len(self._heap)

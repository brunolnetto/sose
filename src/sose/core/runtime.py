from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True, order=True, slots=True)
class ScheduledWork:
    """Durable semantic intent for future command execution."""

    due_at: datetime
    priority: int
    sequence: int
    work_id: str
    command_id: str

    def __init__(
        self,
        work_id: str,
        due_at: datetime,
        priority: int,
        sequence: int,
        command_id: str,
    ) -> None:
        object.__setattr__(self, "due_at", due_at)
        object.__setattr__(self, "priority", priority)
        object.__setattr__(self, "sequence", sequence)
        object.__setattr__(self, "work_id", work_id)
        object.__setattr__(self, "command_id", command_id)


@dataclass(frozen=True, slots=True)
class SimulationPosition:
    """Durable recovery boundary for logical time, tick, and execution ordering."""

    logical_time: datetime
    execution_sequence: int
    committed_sequence: int
    logical_tick: int = 0

    def __post_init__(self) -> None:
        if self.logical_tick < 0:
            raise ValueError("logical_tick must be >= 0")
        if self.execution_sequence < 0:
            raise ValueError("execution_sequence must be >= 0")
        if self.committed_sequence < 0:
            raise ValueError("committed_sequence must be >= 0")
        if self.committed_sequence > self.execution_sequence:
            raise ValueError("committed_sequence cannot exceed execution_sequence")

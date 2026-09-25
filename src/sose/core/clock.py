from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta


@dataclass(slots=True)
class SimulationClock:
    """Logical time owned by the simulator, never by wall-clock sleeps."""

    now: datetime
    step: timedelta
    tick: int = 0

    def advance(self, steps: int = 1) -> datetime:
        if steps < 1:
            raise ValueError("steps must be >= 1")
        self.tick += steps
        self.now += self.step * steps
        return self.now

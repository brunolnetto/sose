from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from .model import AnalyticalBatch


class AnalyticalSink(Protocol):
    def publish(self, batch: AnalyticalBatch) -> None:
        """Publish one deterministic batch idempotently by batch_id."""
        ...


@dataclass(frozen=True, slots=True)
class SinkBinding:
    name: str
    sink: AnalyticalSink

    def __post_init__(self) -> None:
        if not self.name:
            raise ValueError("sink binding name cannot be empty")

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from sose.core.events import DomainEvent


@dataclass(frozen=True, slots=True)
class AnalyticalBatch:
    batch_id: str
    job_id: str
    domain_name: str
    config_revision: int
    logical_tick: int
    logical_time: datetime
    from_event_offset: int
    to_event_offset: int
    events: tuple[DomainEvent, ...]

    def __post_init__(self) -> None:
        if not self.batch_id or not self.job_id or not self.domain_name:
            raise ValueError("analytical batch identity cannot be empty")
        if self.from_event_offset < 0:
            raise ValueError("from_event_offset must be >= 0")
        if self.to_event_offset < self.from_event_offset:
            raise ValueError("invalid analytical batch event range")
        if len(self.events) != self.to_event_offset - self.from_event_offset:
            raise ValueError("analytical batch range does not match event count")


@dataclass(frozen=True, slots=True)
class SinkCheckpoint:
    job_id: str
    sink_name: str
    event_offset: int = 0
    last_delivery_id: str | None = None

    def __post_init__(self) -> None:
        if not self.job_id or not self.sink_name:
            raise ValueError("sink checkpoint identity cannot be empty")
        if self.event_offset < 0:
            raise ValueError("sink checkpoint event_offset must be >= 0")


@dataclass(frozen=True, slots=True)
class SinkDelivery:
    delivery_id: str
    sink_name: str
    batch: AnalyticalBatch
    status: str = "pending"
    attempts: int = 0
    last_error: str | None = None
    delivered_at: datetime | None = None

    def __post_init__(self) -> None:
        if not self.delivery_id or not self.sink_name:
            raise ValueError("sink delivery identity cannot be empty")
        if self.status not in {"pending", "delivered"}:
            raise ValueError(f"unsupported sink delivery status: {self.status}")
        if self.attempts < 0:
            raise ValueError("sink delivery attempts must be >= 0")

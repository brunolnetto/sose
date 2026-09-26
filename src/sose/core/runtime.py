from __future__ import annotations

from dataclasses import dataclass
import math
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


@dataclass(frozen=True, slots=True)
class ResourceDefinition:
    name: str
    capacity: int

    def __post_init__(self) -> None:
        if not self.name:
            raise ValueError("resource name cannot be empty")
        if self.capacity < 1:
            raise ValueError("resource capacity must be >= 1")


@dataclass(frozen=True, order=True, slots=True)
class ResourceDemand:
    priority: int
    sequence: int
    request_id: str
    resource_name: str
    requested_at: datetime

    def __init__(
        self,
        request_id: str,
        resource_name: str,
        priority: int,
        requested_at: datetime,
        sequence: int,
    ) -> None:
        if not request_id:
            raise ValueError("request_id cannot be empty")
        if not resource_name:
            raise ValueError("resource_name cannot be empty")
        object.__setattr__(self, "priority", priority)
        object.__setattr__(self, "sequence", sequence)
        object.__setattr__(self, "request_id", request_id)
        object.__setattr__(self, "resource_name", resource_name)
        object.__setattr__(self, "requested_at", requested_at)


@dataclass(frozen=True, slots=True)
class ResourceReservation:
    reservation_id: str
    request_id: str
    resource_name: str
    acquired_at: datetime
    sequence: int = 0


@dataclass(frozen=True, slots=True)
class ResourceReleaseIntent:
    intent_id: str
    reservation_id: str
    resource_name: str
    requested_at: datetime


@dataclass(frozen=True, slots=True)
class StoreDefinition:
    name: str
    kind: str = "fifo"
    capacity: int | None = None

    def __post_init__(self) -> None:
        if not self.name:
            raise ValueError("store name cannot be empty")
        if self.kind not in {"fifo", "priority", "filter"}:
            raise ValueError("store kind must be one of: fifo, priority, filter")
        if self.capacity is not None and self.capacity < 1:
            raise ValueError("store capacity must be >= 1")


@dataclass(frozen=True, slots=True)
class DurableStoreItem:
    item_id: str
    store_name: str
    value: object
    priority: int
    sequence: int

    def __post_init__(self) -> None:
        if not self.item_id:
            raise ValueError("item_id cannot be empty")
        if not self.store_name:
            raise ValueError("store_name cannot be empty")
        if self.sequence < 1:
            raise ValueError("store item sequence must be >= 1")


@dataclass(frozen=True, slots=True)
class StorePutIntent:
    item_id: str
    store_name: str
    value: object
    priority: int
    requested_at: datetime
    sequence: int

    def __post_init__(self) -> None:
        if not self.item_id:
            raise ValueError("item_id cannot be empty")
        if not self.store_name:
            raise ValueError("store_name cannot be empty")
        if self.sequence < 1:
            raise ValueError("store put sequence must be >= 1")


@dataclass(frozen=True, slots=True)
class StoreGetRequest:
    request_id: str
    store_name: str
    requested_at: datetime
    sequence: int
    filter_key: str | None = None

    def __post_init__(self) -> None:
        if not self.request_id:
            raise ValueError("request_id cannot be empty")
        if not self.store_name:
            raise ValueError("store_name cannot be empty")
        if self.sequence < 1:
            raise ValueError("store get sequence must be >= 1")


@dataclass(frozen=True, slots=True)
class StoreGetResult:
    request_id: str
    store_name: str
    item: DurableStoreItem
    completed_at: datetime
    sequence: int

    def __post_init__(self) -> None:
        if not self.request_id:
            raise ValueError("request_id cannot be empty")
        if not self.store_name:
            raise ValueError("store_name cannot be empty")
        if self.item.store_name != self.store_name:
            raise ValueError("store get result item belongs to a different store")
        if self.sequence < 1:
            raise ValueError("store get result sequence must be >= 1")


@dataclass(frozen=True, slots=True)
class PreemptiveResourceDefinition:
    name: str
    capacity: int

    def __post_init__(self) -> None:
        if not self.name:
            raise ValueError("resource name cannot be empty")
        if self.capacity < 1:
            raise ValueError("resource capacity must be >= 1")


@dataclass(frozen=True, order=True, slots=True)
class PreemptiveResourceDemand:
    priority: int
    sequence: int
    request_id: str
    resource_name: str
    preempt: bool
    requested_at: datetime

    def __init__(
        self,
        request_id: str,
        resource_name: str,
        priority: int,
        preempt: bool,
        requested_at: datetime,
        sequence: int,
    ) -> None:
        if not request_id:
            raise ValueError("request_id cannot be empty")
        if not resource_name:
            raise ValueError("resource_name cannot be empty")
        if sequence < 1:
            raise ValueError("preemptive resource demand sequence must be >= 1")
        object.__setattr__(self, "priority", priority)
        object.__setattr__(self, "sequence", sequence)
        object.__setattr__(self, "request_id", request_id)
        object.__setattr__(self, "resource_name", resource_name)
        object.__setattr__(self, "preempt", preempt)
        object.__setattr__(self, "requested_at", requested_at)


@dataclass(frozen=True, slots=True)
class PreemptiveResourceReservation:
    reservation_id: str
    request_id: str
    resource_name: str
    acquired_at: datetime
    priority: int
    sequence: int

    def __post_init__(self) -> None:
        if not self.reservation_id:
            raise ValueError("reservation_id cannot be empty")
        if not self.request_id:
            raise ValueError("request_id cannot be empty")
        if not self.resource_name:
            raise ValueError("resource_name cannot be empty")
        if self.sequence < 1:
            raise ValueError("preemptive resource reservation sequence must be >= 1")


@dataclass(frozen=True, slots=True)
class PreemptiveResourceReleaseIntent:
    intent_id: str
    reservation_id: str
    resource_name: str
    requested_at: datetime


@dataclass(frozen=True, slots=True)
class ResourcePreemptionResult:
    result_id: str
    resource_name: str
    displaced_reservation_id: str
    displaced_request_id: str
    preempting_request_id: str
    successor_reservation_id: str
    preempted_at: datetime
    sequence: int

    def __post_init__(self) -> None:
        if not self.result_id:
            raise ValueError("preemption result_id cannot be empty")
        if not self.resource_name:
            raise ValueError("resource_name cannot be empty")
        if not self.displaced_reservation_id:
            raise ValueError("displaced_reservation_id cannot be empty")
        if not self.displaced_request_id:
            raise ValueError("displaced_request_id cannot be empty")
        if not self.preempting_request_id:
            raise ValueError("preempting_request_id cannot be empty")
        if not self.successor_reservation_id:
            raise ValueError("successor_reservation_id cannot be empty")
        if self.sequence < 1:
            raise ValueError("preemption result sequence must be >= 1")


@dataclass(frozen=True, slots=True)
class ContainerDefinition:
    name: str
    capacity: float
    initial: float = 0.0

    def __post_init__(self) -> None:
        if not self.name:
            raise ValueError("container name cannot be empty")
        if not math.isfinite(self.capacity) or self.capacity <= 0:
            raise ValueError("container capacity must be finite and > 0")
        if (
            not math.isfinite(self.initial)
            or self.initial < 0
            or self.initial > self.capacity
        ):
            raise ValueError(
                "container initial level must be finite and between 0 and capacity"
            )


@dataclass(frozen=True, slots=True)
class ContainerState:
    name: str
    level: float

    def __post_init__(self) -> None:
        if not self.name:
            raise ValueError("container name cannot be empty")
        if not math.isfinite(self.level) or self.level < 0:
            raise ValueError("container level must be finite and >= 0")


@dataclass(frozen=True, slots=True)
class ContainerOperationIntent:
    request_id: str
    container_name: str
    operation: str
    amount: float
    requested_at: datetime
    sequence: int

    def __post_init__(self) -> None:
        if not self.request_id:
            raise ValueError("request_id cannot be empty")
        if not self.container_name:
            raise ValueError("container_name cannot be empty")
        if self.operation not in {"put", "get"}:
            raise ValueError("container operation must be put or get")
        if not math.isfinite(self.amount) or self.amount <= 0:
            raise ValueError("container amount must be finite and > 0")
        if self.sequence < 1:
            raise ValueError("container operation sequence must be >= 1")


@dataclass(frozen=True, slots=True)
class ContainerOperationResult:
    request_id: str
    container_name: str
    operation: str
    amount: float
    completed_at: datetime
    level_before: float
    level_after: float
    sequence: int

    def __post_init__(self) -> None:
        if not self.request_id:
            raise ValueError("request_id cannot be empty")
        if not self.container_name:
            raise ValueError("container_name cannot be empty")
        if self.operation not in {"put", "get"}:
            raise ValueError("container operation must be put or get")
        if not math.isfinite(self.amount) or self.amount <= 0:
            raise ValueError("container amount must be finite and > 0")
        if (
            not math.isfinite(self.level_before)
            or not math.isfinite(self.level_after)
            or self.level_before < 0
            or self.level_after < 0
        ):
            raise ValueError("container levels must be finite and >= 0")
        if self.sequence < 1:
            raise ValueError("container result sequence must be >= 1")

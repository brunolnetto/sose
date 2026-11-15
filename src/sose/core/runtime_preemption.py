"""Durable preemptive-resource record types, split from ``sose.core.runtime``."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


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



# Defined in ``sose.core.runtime`` before the split and still re-exported from it.
# ``__module__`` stays pinned to that public home so class paths (codec, pickle,
# reprs) are unchanged by the move.
for _moved in (
    PreemptiveResourceDefinition,
    PreemptiveResourceDemand,
    PreemptiveResourceReservation,
    PreemptiveResourceReleaseIntent,
    ResourcePreemptionResult,
):
    _moved.__module__ = "sose.core.runtime"
del _moved

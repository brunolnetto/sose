from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Protocol, runtime_checkable


Callback = Callable[[], None]


@dataclass(frozen=True, slots=True)
class ScheduledCall:
    """Public handle for ephemeral backend scheduling."""

    call_id: str
    due_at: datetime
    priority: int
    sequence: int


@dataclass(frozen=True, slots=True)
class ResourceRequest:
    """Backend-neutral resource request metadata."""

    request_id: str
    resource_name: str
    priority: int
    requested_at: datetime


@dataclass(frozen=True, slots=True)
class ResourceLease:
    """Granted resource usage without exposing backend-native request objects."""

    lease_id: str
    request_id: str
    resource_name: str
    acquired_at: datetime


@dataclass(frozen=True, slots=True)
class ResourcePreemption:
    """Backend-neutral notification that an acquired lease was preempted."""

    lease_id: str
    request_id: str
    resource_name: str
    preempted_at: datetime
    preempted_by: str | None = None


@dataclass(frozen=True, slots=True)
class ResourceSnapshot:
    name: str
    capacity: int
    in_use: int
    queued: int


@dataclass(frozen=True, slots=True)
class ContainerRequest:
    request_id: str
    container_name: str
    operation: str
    amount: float
    requested_at: datetime


@dataclass(frozen=True, slots=True)
class ContainerSnapshot:
    name: str
    capacity: float
    level: float
    queued_puts: int
    queued_gets: int


@runtime_checkable
class TemporalBackend(Protocol):
    @property
    def now(self) -> datetime: ...

    def schedule_at(
        self,
        at: datetime,
        callback: Callback,
        *,
        priority: int = 100,
        key: tuple[object, ...] | None = None,
    ) -> ScheduledCall: ...

    def schedule_after(
        self,
        delay: timedelta,
        callback: Callback,
        *,
        priority: int = 100,
        key: tuple[object, ...] | None = None,
    ) -> ScheduledCall: ...

    def cancel(self, call: ScheduledCall | str) -> bool: ...

    def step(self) -> bool: ...

    def run_until(self, at: datetime) -> int: ...


@runtime_checkable
class ResourceBackend(Protocol):
    def create_resource(self, name: str, *, capacity: int = 1) -> None: ...

    def request_resource(
        self,
        name: str,
        *,
        request_id: str,
        on_acquired: Callable[[ResourceLease], None],
        priority: int = 100,
    ) -> ResourceRequest: ...

    def release_resource(self, lease: ResourceLease | str) -> None: ...

    def resource_snapshot(self, name: str) -> ResourceSnapshot: ...


@runtime_checkable
class ContainerBackend(Protocol):
    def create_container(
        self,
        name: str,
        *,
        capacity: float,
        initial: float = 0.0,
    ) -> None: ...

    def put_container(
        self,
        name: str,
        *,
        request_id: str,
        amount: float,
        on_completed: Callable[[ContainerRequest], None],
    ) -> ContainerRequest: ...

    def get_container(
        self,
        name: str,
        *,
        request_id: str,
        amount: float,
        on_completed: Callable[[ContainerRequest], None],
    ) -> ContainerRequest: ...

    def container_snapshot(self, name: str) -> ContainerSnapshot: ...


@runtime_checkable
class SimulationBackend(TemporalBackend, ResourceBackend, Protocol):
    """Execution mechanics behind SOSE domain semantics."""

    @property
    def name(self) -> str: ...

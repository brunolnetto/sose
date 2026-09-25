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
class StoreItem:
    item_id: str
    store_name: str
    value: object
    priority: int = 100


@dataclass(frozen=True, slots=True)
class StoreRequest:
    request_id: str
    store_name: str
    requested_at: datetime


@dataclass(frozen=True, slots=True)
class StoreSnapshot:
    name: str
    capacity: int | None
    size: int
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
class StoreBackend(Protocol):
    def create_store(self, name: str, *, capacity: int | None = None) -> None: ...

    def create_priority_store(self, name: str, *, capacity: int | None = None) -> None: ...

    def create_filter_store(self, name: str, *, capacity: int | None = None) -> None: ...

    def put_store(
        self,
        name: str,
        *,
        item_id: str,
        value: object,
        priority: int = 100,
        on_stored: Callable[[StoreItem], None] | None = None,
    ) -> StoreItem: ...

    def get_store(
        self,
        name: str,
        *,
        request_id: str,
        on_received: Callable[[StoreItem], None],
        filter: Callable[[StoreItem], bool] | None = None,
    ) -> StoreRequest: ...

    def store_snapshot(self, name: str) -> StoreSnapshot: ...


@runtime_checkable
class SimulationBackend(TemporalBackend, ResourceBackend, Protocol):
    """Execution mechanics behind SOSE domain semantics."""

    @property
    def name(self) -> str: ...

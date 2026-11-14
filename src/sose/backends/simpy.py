from __future__ import annotations

import math
from collections.abc import Callable
from dataclasses import dataclass, field  # noqa: F401
from datetime import datetime, timedelta, timezone

try:
    import simpy
    from simpy.events import Event
    from simpy.resources.container import Container  # noqa: F401
    from simpy.resources.resource import (  # noqa: F401
        PreemptiveResource,
        PriorityRequest,
        PriorityResource,
    )
    from simpy.resources.store import FilterStore, PriorityStore, Store  # noqa: F401
except ImportError as exc:
    raise ImportError(
        "SimPyBackend requires the optional 'simpy' dependency. "
        "Install SOSE with: pip install 'sose[simpy]'"
    ) from exc

from sose.core.identity import deterministic_id

from .base import (  # noqa: F401
    ContainerRequest,
    ContainerSnapshot,
    ResourceLease,
    ResourcePreemption,
    ResourceRequest,
    ResourceSnapshot,
    ScheduledCall,
    StoreItem,
    StoreRequest,
    StoreSnapshot,
)
from .simpy_containers import _ContainerState, _SimPyContainerOperations
from .simpy_preemptive import (
    _PreemptiveLeaseState,
    _PreemptiveResourceState,
    _SimPyPreemptiveOperations,
)
from .simpy_resources import _LeaseState, _ResourceState, _SimPyResourceOperations
from .simpy_stores import _PriorityEnvelope, _SimPyStoreOperations, _StoreState  # noqa: F401


class _ScheduledCallback(Event):
    """Priority-aware delayed callback event isolated behind the SimPy adapter.

    SimPy's public Environment.schedule() supports priority, while Timeout does not
    expose a priority argument. Like SimPy's own Timeout implementation, this event
    is created already triggered and inserted into the environment queue directly.
    """

    def __init__(
        self,
        env: simpy.Environment,
        *,
        delay: float,
        priority: int,
        callback: Callable[[], None],
    ) -> None:
        if delay < 0:
            raise ValueError("delay cannot be negative")
        super().__init__(env)
        self._ok = True
        self._value = None
        self.callbacks.append(lambda _: callback())
        env.schedule(self, priority=priority, delay=delay)


class SimPyBackend(
    _SimPyStoreOperations,
    _SimPyContainerOperations,
    _SimPyResourceOperations,
    _SimPyPreemptiveOperations,
):
    """Discrete-event execution backend powered by SimPy.

    SimPy remains an in-memory execution mechanism. Durable SOSE state, commands,
    schedules and resource reservations are deliberately outside this adapter.
    """

    def __init__(self, *, origin: datetime) -> None:
        self._origin = origin
        self._env = simpy.Environment(initial_time=0.0)
        self._sequence = 0
        self._scheduled: dict[str, ScheduledCall] = {}
        self._cancelled: set[str] = set()
        self._resources: dict[str, _ResourceState] = {}
        self._leases: dict[str, _LeaseState] = {}
        self._request_to_lease: dict[str, str] = {}
        self._containers: dict[str, _ContainerState] = {}
        self._container_request_ids: set[str] = set()
        self._stores: dict[str, _StoreState] = {}
        self._store_request_ids: set[str] = set()
        self._store_sequence = 0
        self._preemptive_resources: dict[str, _PreemptiveResourceState] = {}
        self._preemptive_leases: dict[str, _PreemptiveLeaseState] = {}
        self._preemptive_process_to_request: dict[object, str] = {}
        self._scheduled_preemptive_requests: set[str] = set()
        self._cancelled_preemptive_requests: set[str] = set()

    @property
    def name(self) -> str:
        return "simpy"

    @property
    def now(self) -> datetime:
        return self._from_sim_time(float(self._env.now))

    def schedule_at(
        self,
        at: datetime,
        callback: Callable[[], None],
        *,
        priority: int = 100,
        key: tuple[object, ...] | None = None,
    ) -> ScheduledCall:
        target = self._to_sim_time(at)
        current = float(self._env.now)
        if target < current:
            raise ValueError("cannot schedule work in the past")

        self._sequence += 1
        identity_parts = key or (at, priority, self._sequence)
        call_id = deterministic_id("backend-call", *identity_parts)
        if call_id in self._scheduled:
            raise ValueError(f"scheduled call already exists: {call_id}")

        call = ScheduledCall(
            call_id=call_id,
            due_at=at,
            priority=priority,
            sequence=self._sequence,
        )
        self._scheduled[call_id] = call

        def execute() -> None:
            self._scheduled.pop(call_id, None)
            if call_id in self._cancelled:
                self._cancelled.discard(call_id)
                return
            callback()

        _ScheduledCallback(
            self._env,
            delay=target - current,
            priority=priority,
            callback=execute,
        )
        return call

    def schedule_after(
        self,
        delay: timedelta,
        callback: Callable[[], None],
        *,
        priority: int = 100,
        key: tuple[object, ...] | None = None,
    ) -> ScheduledCall:
        if delay < timedelta(0):
            raise ValueError("delay cannot be negative")
        return self.schedule_at(
            self.now + delay,
            callback,
            priority=priority,
            key=key,
        )

    def cancel(self, call: ScheduledCall | str) -> bool:
        call_id = call.call_id if isinstance(call, ScheduledCall) else call
        if call_id not in self._scheduled:
            return False
        self._cancelled.add(call_id)
        return True

    def step(self) -> bool:
        if math.isinf(float(self._env.peek())):
            return False
        self._env.step()
        return True

    def run_until(self, at: datetime) -> int:
        target = self._to_sim_time(at)
        if target < float(self._env.now):
            raise ValueError("cannot run backwards in simulation time")

        processed = 0
        while float(self._env.peek()) <= target:
            self._env.step()
            processed += 1

        if float(self._env.now) < target:
            # Advance the SimPy clock to the requested boundary only after all
            # events at that exact timestamp have been processed.
            self._env.timeout(target - float(self._env.now))
            self._env.step()
            processed += 1

        return processed

    def _to_sim_time(self, value: datetime) -> float:
        origin_aware = self._origin.tzinfo is not None and self._origin.utcoffset() is not None
        value_aware = value.tzinfo is not None and value.utcoffset() is not None
        if origin_aware != value_aware:
            raise ValueError("backend datetimes must have consistent timezone awareness")
        if origin_aware:
            origin = self._origin.astimezone(timezone.utc)
            target = value.astimezone(timezone.utc)
            return (target - origin).total_seconds()
        return (value - self._origin).total_seconds()

    def _from_sim_time(self, value: float) -> datetime:
        if self._origin.tzinfo is not None and self._origin.utcoffset() is not None:
            instant = self._origin.astimezone(timezone.utc) + timedelta(seconds=value)
            return instant.astimezone(self._origin.tzinfo)
        return self._origin + timedelta(seconds=value)

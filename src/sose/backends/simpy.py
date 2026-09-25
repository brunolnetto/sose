from __future__ import annotations

import math
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

try:
    import simpy
    from simpy.events import Event
    from simpy.resources.resource import PriorityRequest, PriorityResource
except ImportError as exc:  # pragma: no cover - exercised in packaging environments
    raise ImportError(
        "SimPyBackend requires the optional 'simpy' dependency. "
        "Install SOSE with: pip install 'sose[simpy]'"
    ) from exc

from sose.core.identity import deterministic_id

from .base import ResourceLease, ResourceRequest, ResourceSnapshot, ScheduledCall


@dataclass(slots=True)
class _ResourceState:
    resource: PriorityResource
    requests: dict[str, PriorityRequest]


@dataclass(slots=True)
class _LeaseState:
    resource_name: str
    request: PriorityRequest
    lease: ResourceLease


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


class SimPyBackend:
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

    def create_resource(self, name: str, *, capacity: int = 1) -> None:
        if not name:
            raise ValueError("resource name cannot be empty")
        if capacity < 1:
            raise ValueError("resource capacity must be >= 1")
        if name in self._resources:
            raise ValueError(f"resource already exists: {name}")
        self._resources[name] = _ResourceState(
            resource=PriorityResource(self._env, capacity=capacity),
            requests={},
        )

    def request_resource(
        self,
        name: str,
        *,
        request_id: str,
        on_acquired: Callable[[ResourceLease], None],
        priority: int = 100,
    ) -> ResourceRequest:
        state = self._resource(name)
        if not request_id:
            raise ValueError("request_id cannot be empty")
        if (
            any(request_id in resource_state.requests for resource_state in self._resources.values())
            or request_id in self._request_to_lease
        ):
            raise ValueError(f"resource request already exists: {request_id}")

        requested_at = self.now
        public_request = ResourceRequest(
            request_id=request_id,
            resource_name=name,
            priority=priority,
            requested_at=requested_at,
        )
        request = state.resource.request(priority=priority)
        state.requests[request_id] = request

        def granted(_: Event) -> None:
            # Request may have been granted immediately but callbacks are still
            # processed by the environment, keeping acquisition ordering explicit.
            lease = ResourceLease(
                lease_id=deterministic_id("resource-lease", name, request_id),
                request_id=request_id,
                resource_name=name,
                acquired_at=self.now,
            )
            self._leases[lease.lease_id] = _LeaseState(
                resource_name=name,
                request=request,
                lease=lease,
            )
            self._request_to_lease[request_id] = lease.lease_id
            on_acquired(lease)

        if request.callbacks is None:  # pragma: no cover - defensive SimPy boundary
            raise RuntimeError("resource request was processed before callback attachment")
        request.callbacks.append(granted)
        return public_request

    def release_resource(self, lease: ResourceLease | str) -> None:
        lease_id = lease.lease_id if isinstance(lease, ResourceLease) else lease
        state = self._leases.pop(lease_id, None)
        if state is None:
            raise KeyError(f"unknown resource lease: {lease_id}")

        resource = self._resource(state.resource_name)
        resource.resource.release(state.request)
        resource.requests.pop(state.lease.request_id, None)
        self._request_to_lease.pop(state.lease.request_id, None)

    def resource_snapshot(self, name: str) -> ResourceSnapshot:
        state = self._resource(name)
        resource = state.resource
        return ResourceSnapshot(
            name=name,
            capacity=resource.capacity,
            in_use=resource.count,
            queued=len(resource.queue),
        )

    def _resource(self, name: str) -> _ResourceState:
        try:
            return self._resources[name]
        except KeyError as exc:
            raise KeyError(f"unknown resource: {name}") from exc

    def _to_sim_time(self, value: datetime) -> float:
        origin_aware = self._origin.tzinfo is not None and self._origin.utcoffset() is not None
        value_aware = value.tzinfo is not None and value.utcoffset() is not None
        if origin_aware != value_aware:
            raise ValueError("backend datetimes must have consistent timezone awareness")
        return (value - self._origin).total_seconds()

    def _from_sim_time(self, value: float) -> datetime:
        return self._origin + timedelta(seconds=value)

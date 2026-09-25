from __future__ import annotations

import math
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

try:
    import simpy
    from simpy.events import Event
    from simpy.resources.container import Container
    from simpy.resources.resource import PreemptiveResource, PriorityRequest, PriorityResource
    from simpy.resources.store import FilterStore, PriorityStore, Store
except ImportError as exc:  # pragma: no cover - exercised in packaging environments
    raise ImportError(
        "SimPyBackend requires the optional 'simpy' dependency. "
        "Install SOSE with: pip install 'sose[simpy]'"
    ) from exc

from sose.core.identity import deterministic_id

from .base import (
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


@dataclass(order=True, slots=True)
class _PriorityEnvelope:
    priority: int
    sequence: int
    item: StoreItem = field(compare=False)


@dataclass(slots=True)
class _StoreState:
    store: Store
    kind: str
    items: dict[str, StoreItem]


@dataclass(slots=True)
class _ContainerState:
    container: Container


@dataclass(slots=True)
class _ResourceState:
    resource: PriorityResource
    requests: dict[str, PriorityRequest]


@dataclass(slots=True)
class _LeaseState:
    resource_name: str
    request: PriorityRequest
    lease: ResourceLease


@dataclass(slots=True)
class _PreemptiveResourceState:
    resource: PreemptiveResource
    requests: dict[str, PriorityRequest]
    holds: dict[str, Event]


@dataclass(slots=True)
class _PreemptiveLeaseState:
    resource_name: str
    request: PriorityRequest
    lease: ResourceLease
    hold: Event


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
        self._containers: dict[str, _ContainerState] = {}
        self._container_request_ids: set[str] = set()
        self._stores: dict[str, _StoreState] = {}
        self._store_request_ids: set[str] = set()
        self._store_sequence = 0
        self._preemptive_resources: dict[str, _PreemptiveResourceState] = {}
        self._preemptive_leases: dict[str, _PreemptiveLeaseState] = {}
        self._preemptive_process_to_request: dict[object, str] = {}

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



    def create_store(self, name: str, *, capacity: int | None = None) -> None:
        self._create_store(name, kind="fifo", capacity=capacity)

    def create_priority_store(self, name: str, *, capacity: int | None = None) -> None:
        self._create_store(name, kind="priority", capacity=capacity)

    def create_filter_store(self, name: str, *, capacity: int | None = None) -> None:
        self._create_store(name, kind="filter", capacity=capacity)

    def put_store(
        self,
        name: str,
        *,
        item_id: str,
        value: object,
        priority: int = 100,
        on_stored: Callable[[StoreItem], None] | None = None,
    ) -> StoreItem:
        state = self._store(name)
        if not item_id:
            raise ValueError("item_id cannot be empty")
        if any(item_id in current.items for current in self._stores.values()):
            raise ValueError(f"store item already exists: {item_id}")

        item = StoreItem(
            item_id=item_id,
            store_name=name,
            value=value,
            priority=priority,
        )
        state.items[item_id] = item

        native_item: object = item
        if state.kind == "priority":
            self._store_sequence += 1
            native_item = _PriorityEnvelope(priority, self._store_sequence, item)

        event = state.store.put(native_item)
        if on_stored is not None:
            if event.callbacks is None:  # pragma: no cover - defensive SimPy boundary
                raise RuntimeError("store put was processed before callback attachment")
            event.callbacks.append(lambda _: on_stored(item))
        return item

    def get_store(
        self,
        name: str,
        *,
        request_id: str,
        on_received: Callable[[StoreItem], None],
        filter: Callable[[StoreItem], bool] | None = None,
    ) -> StoreRequest:
        state = self._store(name)
        if not request_id:
            raise ValueError("request_id cannot be empty")
        if request_id in self._store_request_ids:
            raise ValueError(f"store request already exists: {request_id}")
        self._store_request_ids.add(request_id)

        public_request = StoreRequest(
            request_id=request_id,
            store_name=name,
            requested_at=self.now,
        )

        if filter is not None:
            if state.kind != "filter":
                raise ValueError("filters are supported only by filter stores")
            event = state.store.get(filter=lambda native: filter(
                native.item if isinstance(native, _PriorityEnvelope) else native
            ))
        else:
            event = state.store.get()

        def received(completed: Event) -> None:
            native_item = completed.value
            item = native_item.item if isinstance(native_item, _PriorityEnvelope) else native_item
            if not isinstance(item, StoreItem):  # pragma: no cover - adapter invariant
                raise RuntimeError("store returned a non-SOSE item")
            state.items.pop(item.item_id, None)
            on_received(item)

        if event.callbacks is None:  # pragma: no cover - defensive SimPy boundary
            raise RuntimeError("store get was processed before callback attachment")
        event.callbacks.append(received)
        return public_request

    def store_snapshot(self, name: str) -> StoreSnapshot:
        state = self._store(name)
        capacity = None if math.isinf(float(state.store.capacity)) else int(state.store.capacity)
        return StoreSnapshot(
            name=name,
            capacity=capacity,
            size=len(state.store.items),
            queued_puts=len(state.store.put_queue),
            queued_gets=len(state.store.get_queue),
        )

    def _create_store(self, name: str, *, kind: str, capacity: int | None) -> None:
        if not name:
            raise ValueError("store name cannot be empty")
        if capacity is not None and capacity < 1:
            raise ValueError("store capacity must be >= 1")
        if name in self._stores:
            raise ValueError(f"store already exists: {name}")

        kwargs = {} if capacity is None else {"capacity": capacity}
        if kind == "fifo":
            store = Store(self._env, **kwargs)
        elif kind == "priority":
            store = PriorityStore(self._env, **kwargs)
        elif kind == "filter":
            store = FilterStore(self._env, **kwargs)
        else:  # pragma: no cover - internal invariant
            raise ValueError(f"unknown store kind: {kind}")
        self._stores[name] = _StoreState(store=store, kind=kind, items={})

    def _store(self, name: str) -> _StoreState:
        try:
            return self._stores[name]
        except KeyError as exc:
            raise KeyError(f"unknown store: {name}") from exc

    def create_container(
        self,
        name: str,
        *,
        capacity: float,
        initial: float = 0.0,
    ) -> None:
        if not name:
            raise ValueError("container name cannot be empty")
        if capacity <= 0:
            raise ValueError("container capacity must be > 0")
        if initial < 0 or initial > capacity:
            raise ValueError("container initial level must be between 0 and capacity")
        if name in self._containers:
            raise ValueError(f"container already exists: {name}")
        self._containers[name] = _ContainerState(
            container=Container(self._env, capacity=capacity, init=initial)
        )

    def put_container(
        self,
        name: str,
        *,
        request_id: str,
        amount: float,
        on_completed: Callable[[ContainerRequest], None],
    ) -> ContainerRequest:
        return self._container_operation(
            name,
            request_id=request_id,
            amount=amount,
            operation="put",
            on_completed=on_completed,
        )

    def get_container(
        self,
        name: str,
        *,
        request_id: str,
        amount: float,
        on_completed: Callable[[ContainerRequest], None],
    ) -> ContainerRequest:
        return self._container_operation(
            name,
            request_id=request_id,
            amount=amount,
            operation="get",
            on_completed=on_completed,
        )

    def container_snapshot(self, name: str) -> ContainerSnapshot:
        state = self._container(name)
        container = state.container
        return ContainerSnapshot(
            name=name,
            capacity=float(container.capacity),
            level=float(container.level),
            queued_puts=len(container.put_queue),
            queued_gets=len(container.get_queue),
        )

    def _container_operation(
        self,
        name: str,
        *,
        request_id: str,
        amount: float,
        operation: str,
        on_completed: Callable[[ContainerRequest], None],
    ) -> ContainerRequest:
        state = self._container(name)
        if not request_id:
            raise ValueError("request_id cannot be empty")
        if request_id in self._container_request_ids:
            raise ValueError(f"container request already exists: {request_id}")
        if amount <= 0:
            raise ValueError("container amount must be > 0")

        request = ContainerRequest(
            request_id=request_id,
            container_name=name,
            operation=operation,
            amount=float(amount),
            requested_at=self.now,
        )
        self._container_request_ids.add(request_id)

        if operation == "put":
            event = state.container.put(amount)
        elif operation == "get":
            event = state.container.get(amount)
        else:  # pragma: no cover - internal invariant
            raise ValueError(f"unknown container operation: {operation}")

        if event.callbacks is None:  # pragma: no cover - defensive SimPy boundary
            raise RuntimeError("container operation was processed before callback attachment")
        event.callbacks.append(lambda _: on_completed(request))
        return request

    def _container(self, name: str) -> _ContainerState:
        try:
            return self._containers[name]
        except KeyError as exc:
            raise KeyError(f"unknown container: {name}") from exc

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

    def create_preemptive_resource(self, name: str, *, capacity: int = 1) -> None:
        if not name:
            raise ValueError("resource name cannot be empty")
        if capacity < 1:
            raise ValueError("resource capacity must be >= 1")
        if name in self._resources or name in self._preemptive_resources:
            raise ValueError(f"resource already exists: {name}")
        self._preemptive_resources[name] = _PreemptiveResourceState(
            resource=PreemptiveResource(self._env, capacity=capacity),
            requests={},
            holds={},
        )

    def request_preemptive_resource(
        self,
        name: str,
        *,
        request_id: str,
        on_acquired: Callable[[ResourceLease], None],
        on_preempted: Callable[[ResourcePreemption], None],
        priority: int = 100,
        preempt: bool = True,
    ) -> ResourceRequest:
        state = self._preemptive_resource(name)
        if not request_id:
            raise ValueError("request_id cannot be empty")
        if (
            any(request_id in resource_state.requests for resource_state in self._resources.values())
            or any(
                request_id in resource_state.requests
                for resource_state in self._preemptive_resources.values()
            )
            or request_id in self._request_to_lease
        ):
            raise ValueError(f"resource request already exists: {request_id}")

        public_request = ResourceRequest(
            request_id=request_id,
            resource_name=name,
            priority=priority,
            requested_at=self.now,
        )

        def lifecycle():
            process = self._env.active_process
            if process is None:  # pragma: no cover - SimPy process invariant
                raise RuntimeError("preemptive resource request has no active process")
            self._preemptive_process_to_request[process] = request_id
            request = state.resource.request(priority=priority, preempt=preempt)
            state.requests[request_id] = request
            lease_id: str | None = None
            try:
                yield request
                lease = ResourceLease(
                    lease_id=deterministic_id("resource-lease", name, request_id),
                    request_id=request_id,
                    resource_name=name,
                    acquired_at=self.now,
                )
                hold = self._env.event()
                state.holds[request_id] = hold
                self._preemptive_leases[lease.lease_id] = _PreemptiveLeaseState(
                    resource_name=name,
                    request=request,
                    lease=lease,
                    hold=hold,
                )
                self._request_to_lease[request_id] = lease.lease_id
                lease_id = lease.lease_id
                on_acquired(lease)
                try:
                    yield hold
                except simpy.Interrupt as interrupt:
                    on_preempted(
                        ResourcePreemption(
                            lease_id=lease.lease_id,
                            request_id=request_id,
                            resource_name=name,
                            preempted_at=self.now,
                            preempted_by=self._preemptive_process_to_request.get(
                                getattr(interrupt.cause, "by", None)
                            ),
                        )
                    )
            finally:
                state.requests.pop(request_id, None)
                state.holds.pop(request_id, None)
                self._preemptive_process_to_request.pop(process, None)
                if lease_id is not None:
                    self._preemptive_leases.pop(lease_id, None)
                    self._request_to_lease.pop(request_id, None)
                if request in state.resource.users:
                    state.resource.release(request)

        self._env.process(lifecycle())
        return public_request

    def release_preemptive_resource(self, lease: ResourceLease | str) -> None:
        lease_id = lease.lease_id if isinstance(lease, ResourceLease) else lease
        state = self._preemptive_leases.get(lease_id)
        if state is None:
            raise KeyError(f"unknown preemptive resource lease: {lease_id}")
        if not state.hold.triggered:
            state.hold.succeed()

    def preemptive_resource_snapshot(self, name: str) -> ResourceSnapshot:
        state = self._preemptive_resource(name)
        resource = state.resource
        return ResourceSnapshot(
            name=name,
            capacity=resource.capacity,
            in_use=resource.count,
            queued=len(resource.queue),
        )

    def _preemptive_resource(self, name: str) -> _PreemptiveResourceState:
        try:
            return self._preemptive_resources[name]
        except KeyError as exc:
            raise KeyError(f"unknown preemptive resource: {name}") from exc

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

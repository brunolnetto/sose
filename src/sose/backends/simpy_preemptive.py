"""SimPy preemptive-resource operations of ``SimPyBackend``, split from ``sose.backends.simpy``."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

import simpy
from simpy.events import Event
from simpy.resources.resource import PreemptiveResource, PriorityRequest

from sose.core.identity import deterministic_id

from .base import ResourceLease, ResourcePreemption, ResourceRequest, ResourceSnapshot


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


class _SimPyPreemptiveOperations:
    """Preemptive resource methods mixed into ``SimPyBackend``."""

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
            or request_id in self._scheduled_preemptive_requests
        ):
            raise ValueError(f"resource request already exists: {request_id}")

        public_request = ResourceRequest(
            request_id=request_id,
            resource_name=name,
            priority=priority,
            requested_at=self.now,
        )

        self._scheduled_preemptive_requests.add(request_id)

        def lifecycle():
            process = self._env.active_process
            if process is None:  # pragma: no cover - SimPy process invariant
                raise RuntimeError("preemptive resource request has no active process")
            self._preemptive_process_to_request[process] = request_id
            if request_id in self._cancelled_preemptive_requests:
                self._cancelled_preemptive_requests.discard(request_id)
                self._scheduled_preemptive_requests.discard(request_id)
                self._preemptive_process_to_request.pop(process, None)
                return
            request = state.resource.request(priority=priority, preempt=preempt)
            state.requests[request_id] = request
            self._scheduled_preemptive_requests.discard(request_id)
            lease_id: str | None = None
            try:
                yield request
                if request_id in self._cancelled_preemptive_requests:
                    self._cancelled_preemptive_requests.discard(request_id)
                    return
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
                self._scheduled_preemptive_requests.discard(request_id)
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

    def cancel_preemptive_resource_request(self, request_id: str) -> bool:
        lease_id = self._request_to_lease.get(request_id)
        if lease_id is not None:
            # A preemptive grant may be backend-acquired while durable truth still
            # remains a demand during the two-callback preemption handshake.
            self.release_preemptive_resource(lease_id)
            return True

        for state in self._preemptive_resources.values():
            request = state.requests.get(request_id)
            if request is None:
                continue
            self._cancelled_preemptive_requests.add(request_id)
            if not request.triggered:
                request.cancel()
                # Wake the waiting lifecycle so its finally block can terminate
                # without ever owning capacity.
                request.succeed()
            elif request in state.resource.users:
                state.resource.release(request)
            state.requests.pop(request_id, None)
            return True

        # A lifecycle can be scheduled but not yet have registered its backend
        # request. Only known scheduled IDs may create a cancellation tombstone.
        if request_id in self._scheduled_preemptive_requests:
            self._cancelled_preemptive_requests.add(request_id)
            return True
        return False

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

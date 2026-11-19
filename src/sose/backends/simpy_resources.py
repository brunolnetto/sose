"""SimPy priority-resource operations of ``SimPyBackend``, split from ``sose.backends.simpy``."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from simpy.events import Event
from simpy.resources.resource import PriorityRequest, PriorityResource

from sose.core.identity import deterministic_id

from .base import ResourceLease, ResourceRequest, ResourceSnapshot


@dataclass(slots=True)
class _ResourceState:
    resource: PriorityResource
    requests: dict[str, PriorityRequest]


@dataclass(slots=True)
class _LeaseState:
    resource_name: str
    request: PriorityRequest
    lease: ResourceLease


class _SimPyResourceOperations:
    """Non-preemptive priority resource methods mixed into ``SimPyBackend``."""

    def create_resource(self, name: str, *, capacity: int = 1) -> None:
        if not name:
            raise ValueError("resource name cannot be empty")
        if capacity < 1:
            raise ValueError("resource capacity must be >= 1")
        if name in self._resources or name in self._preemptive_resources:
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
            or any(
                request_id in resource_state.requests
                for resource_state in self._preemptive_resources.values()
            )
            or request_id in self._request_to_lease
            or request_id in self._scheduled_preemptive_requests
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

    def cancel_resource_request(self, request_id: str) -> bool:
        if request_id in self._request_to_lease:
            return False
        for state in self._resources.values():
            request = state.requests.get(request_id)
            if request is None:
                continue
            if request.triggered:
                # Capacity can be granted synchronously while the callback is still
                # waiting in SimPy's event queue. Remove the callback and release
                # the underlying request before it can become an unmanaged lease.
                if request.callbacks is None:
                    return False
                request.callbacks.clear()
                if request in state.resource.users:
                    state.resource.release(request)
                state.requests.pop(request_id, None)
                return True
            request.cancel()
            state.requests.pop(request_id, None)
            return True
        return False

    def release_resource(self, lease: ResourceLease | str) -> None:
        lease_id = lease.lease_id if isinstance(lease, ResourceLease) else lease
        state = self._leases.pop(lease_id, None)
        if state is None:
            raise KeyError(f"unknown resource lease: {lease_id}")

        resource = self._resource(state.resource_name)
        resource.resource.release(state.request)
        resource.requests.pop(state.lease.request_id, None)
        self._request_to_lease.pop(state.lease.request_id, None)

    def resource_lease_active(
        self, lease_id: str, *, resource_name: str, request_id: str,
    ) -> bool:
        """Prove the EXACT native resource permit is still held.

        Aggregate ResourceSnapshot.in_use is insufficient when capacity > 1:
        another request can occupy the pool after this particular lease has
        ended. This read is deliberately side-effect free and runs during the
        fenced SOSE commit; it never steps the SimPy event queue.
        """
        native = self._leases.get(lease_id)
        state = self._resources.get(resource_name)
        return bool(
            native is not None
            and state is not None
            and native.resource_name == resource_name
            and native.lease.request_id == request_id
            and native.request in state.resource.users
        )

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

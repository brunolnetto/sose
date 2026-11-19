"""SimPy Container operations of ``SimPyBackend``, split from ``sose.backends.simpy``."""

from __future__ import annotations

import math
from collections.abc import Callable
from dataclasses import dataclass

from simpy.resources.container import Container

from .base import ContainerRequest, ContainerSnapshot


@dataclass(slots=True)
class _ContainerState:
    container: Container


class _SimPyContainerOperations:
    """Container methods mixed into ``SimPyBackend``."""

    def create_container(
        self,
        name: str,
        *,
        capacity: float,
        initial: float = 0.0,
    ) -> None:
        if not name:
            raise ValueError("container name cannot be empty")
        if not math.isfinite(capacity) or capacity <= 0:
            raise ValueError("container capacity must be finite and > 0")
        if not math.isfinite(initial) or initial < 0 or initial > capacity:
            raise ValueError(
                "container initial level must be finite and between 0 and capacity"
            )
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
        if not math.isfinite(amount) or amount <= 0:
            raise ValueError("container amount must be finite and > 0")

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

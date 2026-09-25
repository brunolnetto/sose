from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from sose.core.identity import deterministic_id
from sose.persistence.base import Persistence


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


class DurableResourceManager:
    """Durable resource truth reconstructed into an ephemeral ResourceBackend."""

    def __init__(self, persistence: Persistence) -> None:
        self._persistence = persistence

    def rebuild_backend(self, backend) -> int:
        for definition in self._persistence.resource_definitions():
            backend.create_resource(definition.name, capacity=definition.capacity)

        restored = 0
        for reservation in self._persistence.resource_reservations():
            backend.request_resource(
                reservation.resource_name,
                request_id=reservation.request_id,
                priority=0,
                on_acquired=lambda _lease: None,
            )
            restored += 1

        for demand in self._persistence.resource_demands():
            backend.request_resource(
                demand.resource_name,
                request_id=demand.request_id,
                priority=demand.priority,
                on_acquired=lambda _lease: None,
            )
            restored += 1
        return restored

    def commit_grant(self, *, request_id: str, acquired_at: datetime) -> ResourceReservation:
        existing = next(
            (
                reservation
                for reservation in self._persistence.resource_reservations()
                if reservation.request_id == request_id
            ),
            None,
        )
        if existing is not None:
            return existing

        demand = next(
            (demand for demand in self._persistence.resource_demands() if demand.request_id == request_id),
            None,
        )
        if demand is None:
            raise KeyError(f"unknown resource demand: {request_id}")

        reservation = ResourceReservation(
            reservation_id=deterministic_id(
                "resource-reservation",
                demand.resource_name,
                demand.request_id,
            ),
            request_id=demand.request_id,
            resource_name=demand.resource_name,
            acquired_at=acquired_at,
        )
        with self._persistence.transaction() as uow:
            persisted = uow.get_resource_demand(request_id)
            if persisted != demand:
                raise RuntimeError(f"resource demand changed before grant: {request_id}")
            uow.delete_resource_demand(request_id)
            uow.save_resource_reservation(reservation)
        return reservation

    def release(self, reservation_id: str) -> bool:
        reservation = next(
            (
                reservation
                for reservation in self._persistence.resource_reservations()
                if reservation.reservation_id == reservation_id
            ),
            None,
        )
        if reservation is None:
            return False
        with self._persistence.transaction() as uow:
            persisted = uow.get_resource_reservation(reservation_id)
            if persisted != reservation:
                return False
            uow.delete_resource_reservation(reservation_id)
        return True

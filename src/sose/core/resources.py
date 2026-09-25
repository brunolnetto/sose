from __future__ import annotations

from datetime import datetime

from sose.core.identity import deterministic_id
from sose.core.runtime import ResourceDefinition, ResourceDemand, ResourceReservation
from sose.persistence.base import Persistence


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

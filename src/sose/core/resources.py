from __future__ import annotations

from datetime import datetime

from sose.core.identity import deterministic_id
from sose.core.runtime import ResourceDefinition, ResourceDemand, ResourceReservation
from sose.persistence.base import Persistence


class DurableResourceManager:
    """Durable resource truth reconstructed into an ephemeral ResourceBackend."""

    def __init__(self, persistence: Persistence) -> None:
        self._persistence = persistence
        self._backend_leases: dict[str, object] = {}

    def rebuild_backend(self, backend) -> int:
        for definition in self._persistence.resource_definitions():
            backend.create_resource(definition.name, capacity=definition.capacity)

        restored = 0
        for reservation in self._persistence.resource_reservations():
            backend.request_resource(
                reservation.resource_name,
                request_id=reservation.request_id,
                priority=0,
                on_acquired=lambda lease, reservation_id=reservation.reservation_id: (
                    self._backend_leases.__setitem__(reservation_id, lease)
                ),
            )
            restored += 1

        for demand in self._persistence.resource_demands():
            backend.request_resource(
                demand.resource_name,
                request_id=demand.request_id,
                priority=demand.priority,
                on_acquired=lambda lease, request_id=demand.request_id: self._record_grant(
                    request_id=request_id,
                    lease=lease,
                ),
            )
            restored += 1
        return restored

    def request(
        self,
        backend,
        *,
        resource_name: str,
        request_id: str,
        requested_at: datetime,
        priority: int = 100,
        on_acquired=None,
    ) -> ResourceDemand:
        definitions = {definition.name for definition in self._persistence.resource_definitions()}
        if resource_name not in definitions:
            raise KeyError(f"unknown resource definition: {resource_name}")
        if any(
            demand.request_id == request_id
            for demand in self._persistence.resource_demands()
        ) or any(
            reservation.request_id == request_id
            for reservation in self._persistence.resource_reservations()
        ):
            raise ValueError(f"resource request already exists: {request_id}")

        sequence = max(
            [
                *(demand.sequence for demand in self._persistence.resource_demands()),
                *(reservation.sequence for reservation in self._persistence.resource_reservations()),
            ],
            default=0,
        ) + 1
        demand = ResourceDemand(
            request_id=request_id,
            resource_name=resource_name,
            priority=priority,
            requested_at=requested_at,
            sequence=sequence,
        )
        with self._persistence.transaction() as uow:
            uow.save_resource_demand(demand)

        def granted(lease) -> None:
            reservation = self._record_grant(
                request_id=request_id,
                lease=lease,
            )
            if on_acquired is not None:
                on_acquired(reservation)

        backend.request_resource(
            resource_name,
            request_id=request_id,
            priority=priority,
            on_acquired=granted,
        )
        return demand

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
            sequence=demand.sequence,
        )
        with self._persistence.transaction() as uow:
            persisted = uow.get_resource_demand(request_id)
            if persisted != demand:
                raise RuntimeError(f"resource demand changed before grant: {request_id}")
            uow.delete_resource_demand(request_id)
            uow.save_resource_reservation(reservation)
        return reservation

    def _record_grant(self, *, request_id: str, lease) -> ResourceReservation:
        reservation = self.commit_grant(
            request_id=request_id,
            acquired_at=lease.acquired_at,
        )
        self._backend_leases[reservation.reservation_id] = lease
        return reservation

    def release(self, backend, reservation_id: str) -> bool:
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

        lease = self._backend_leases.pop(reservation_id, None)
        if lease is None:
            raise RuntimeError(
                f"backend lease is not reconstructed for reservation: {reservation_id}"
            )
        backend.release_resource(lease)
        return True

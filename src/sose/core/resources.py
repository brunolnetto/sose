from __future__ import annotations

from datetime import datetime

from sose.core.identity import deterministic_id
from sose.core.runtime import (
    ResourceDefinition,
    ResourceDemand,
    ResourceReleaseIntent,
    ResourceReservation,
)
from sose.persistence.base import Persistence


class DurableResourceManager:
    """Durable resource truth reconstructed into an ephemeral ResourceBackend."""

    def __init__(self, persistence: Persistence) -> None:
        self._persistence = persistence
        self._backend_leases: dict[str, object] = {}

    def rebuild_backend(self, backend) -> int:
        self._finalize_interrupted_releases()

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

    def reservation_for(self, request_id: str) -> ResourceReservation | None:
        """Return the durable reservation currently owned by a request, if any."""
        return next(
            (
                reservation
                for reservation in self._persistence.resource_reservations()
                if reservation.request_id == request_id
            ),
            None,
        )

    def has_request(self, request_id: str) -> bool:
        """Return whether a request is durably pending or currently reserved."""
        return self.reservation_for(request_id) is not None or any(
            demand.request_id == request_id
            for demand in self._persistence.resource_demands()
        )

    def ensure_requested(
        self,
        backend,
        *,
        resource_name: str,
        request_id: str,
        requested_at: datetime,
        priority: int = 100,
    ) -> ResourceReservation | None:
        """Ensure one durable request exists and reconcile immediate acquisition.

        This deliberately does not encode any business decision about whether the
        request *should* exist. Callers decide eligibility first, then use this
        method to make the durable resource intent idempotent across retries.
        """
        if not self.has_request(request_id):
            self.request(
                backend,
                resource_name=resource_name,
                request_id=request_id,
                requested_at=requested_at,
                priority=priority,
            )
        run_until = getattr(backend, "run_until", None)
        if callable(run_until):
            run_until(getattr(backend, "now", requested_at))
        return self.reservation_for(request_id)

    def withdraw(self, backend, request_id: str) -> bool:
        """Remove a durable request regardless of pending/granted phase.

        A request can race from ResourceDemand to ResourceReservation while a
        scenario or post-state reconciliation invalidates the work. Withdrawing
        first cancels pending demand, then releases any reservation that exists
        after cancellation. The operation is idempotent and safe to retry.
        """
        changed = self.cancel_pending(backend, request_id)
        reservation = self.reservation_for(request_id)
        if reservation is not None:
            changed = self.release(backend, reservation.reservation_id) or changed
        run_until = getattr(backend, "run_until", None)
        if callable(run_until):
            run_until(getattr(backend, "now"))
        return changed

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
        existing = self.reservation_for(request_id)
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

    def cancel_pending(self, backend, request_id: str) -> bool:
        demand = next(
            (
                demand
                for demand in self._persistence.resource_demands()
                if demand.request_id == request_id
            ),
            None,
        )
        if demand is None:
            return False

        if not backend.cancel_resource_request(request_id):
            return False

        with self._persistence.transaction() as uow:
            if uow.get_resource_demand(request_id) != demand:
                raise RuntimeError(
                    f"resource demand changed during cancellation: {request_id}"
                )
            uow.delete_resource_demand(request_id)
        return True

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

        lease = self._backend_leases.get(reservation_id)
        if lease is None:
            raise RuntimeError(
                f"backend lease is not reconstructed for reservation: {reservation_id}"
            )

        intent = ResourceReleaseIntent(
            intent_id=deterministic_id("resource-release", reservation_id),
            reservation_id=reservation_id,
            resource_name=reservation.resource_name,
            requested_at=getattr(backend, "now", reservation.acquired_at),
        )
        with self._persistence.transaction() as uow:
            persisted = uow.get_resource_reservation(reservation_id)
            if persisted != reservation:
                return False
            uow.save_resource_release_intent(intent)

        backend.release_resource(lease)

        self._finalize_release_intent(intent, reservation)
        self._backend_leases.pop(reservation_id, None)
        return True

    def _finalize_release_intent(
        self,
        intent: ResourceReleaseIntent,
        reservation: ResourceReservation,
    ) -> None:
        with self._persistence.transaction() as uow:
            persisted_intent = uow.get_resource_release_intent(intent.intent_id)
            persisted_reservation = uow.get_resource_reservation(reservation.reservation_id)
            if persisted_intent != intent:
                raise RuntimeError(
                    f"resource release intent changed: {intent.intent_id}"
                )
            if persisted_reservation != reservation:
                raise RuntimeError(
                    f"resource reservation changed during release: {reservation.reservation_id}"
                )
            uow.delete_resource_reservation(reservation.reservation_id)
            uow.delete_resource_release_intent(intent.intent_id)

    def _finalize_interrupted_releases(self) -> None:
        for intent in self._persistence.resource_release_intents():
            reservation = next(
                (
                    reservation
                    for reservation in self._persistence.resource_reservations()
                    if reservation.reservation_id == intent.reservation_id
                ),
                None,
            )
            if reservation is None:
                with self._persistence.transaction() as uow:
                    uow.delete_resource_release_intent(intent.intent_id)
                continue
            self._finalize_release_intent(intent, reservation)

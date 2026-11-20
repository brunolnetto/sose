from __future__ import annotations

from collections.abc import Callable
from datetime import datetime

from sose.backends.base import ResourceLease, ResourcePreemption
from sose.core.identity import deterministic_id
from sose.core.preemption_handshake import _PreemptiveGrantHandshake
from sose.core.preemption_queries import _PreemptiveDurableQueries
from sose.core.preemption_rebuild import _PreemptiveRebuildValidation
from sose.core.runtime import (
    PreemptiveResourceDefinition,
    PreemptiveResourceDemand,
    PreemptiveResourceReleaseIntent,
    PreemptiveResourceReservation,
)
from sose.persistence.base import Persistence


class DurablePreemptiveResourceManager(
    _PreemptiveGrantHandshake,
    _PreemptiveRebuildValidation,
    _PreemptiveDurableQueries,
):
    """Crash-consistent durable semantics for backend preemptive resources."""

    def __init__(self, persistence: Persistence) -> None:
        self._persistence = persistence
        self._backend_leases: dict[str, ResourceLease] = {}
        self._pending_grants: dict[str, ResourceLease] = {}
        self._pending_preemptions: dict[str, ResourcePreemption] = {}
        self._pending_callbacks: dict[
            str, Callable[[PreemptiveResourceReservation], None]
        ] = {}

    def define(self, definition: PreemptiveResourceDefinition) -> None:
        with self._persistence.transaction() as uow:
            uow.save_preemptive_resource_definition(definition)

    def rebuild_backend(self, backend) -> int:
        self._clear_ephemeral_state()
        self.validate_rebuild()
        self._finalize_interrupted_releases()

        definitions = {
            definition.name: definition
            for definition in self._persistence.preemptive_resource_definitions()
        }
        for definition in definitions.values():
            backend.create_preemptive_resource(
                definition.name,
                capacity=definition.capacity,
            )

        restored = 0
        for reservation in self._persistence.preemptive_resource_reservations():
            backend.request_preemptive_resource(
                reservation.resource_name,
                request_id=reservation.request_id,
                priority=reservation.priority,
                preempt=False,
                on_acquired=lambda lease, request_id=reservation.request_id: (
                    self._record_acquired(request_id=request_id, lease=lease)
                ),
                on_preempted=lambda event: self._record_preempted(event),
            )
            restored += 1

        for demand in self._persistence.preemptive_resource_demands():
            self._submit_demand(backend, demand)
            restored += 1
        return restored

    def ensure_requested(
        self,
        backend,
        *,
        resource_name: str,
        request_id: str,
        requested_at: datetime,
        priority: int = 100,
        preempt: bool = True,
        on_acquired: Callable[[PreemptiveResourceReservation], None] | None = None,
    ) -> PreemptiveResourceReservation | None:
        """Ensure one durable preemptive request exists and reconcile acquisition."""
        if not self.has_request(request_id):
            self.request(
                backend,
                resource_name=resource_name,
                request_id=request_id,
                requested_at=requested_at,
                priority=priority,
                preempt=preempt,
                on_acquired=on_acquired,
            )
        elif (
            on_acquired is not None
            and self.reservation_for(request_id) is None
        ):
            self._pending_callbacks[request_id] = on_acquired
        run_until = getattr(backend, "run_until", None)
        if callable(run_until):
            run_until(getattr(backend, "now", requested_at))
        return self.reservation_for(request_id)

    def withdraw(self, backend, request_id: str) -> bool:
        """Remove a preemptive request regardless of pending/granted phase."""
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
        preempt: bool = True,
        on_acquired: Callable[[PreemptiveResourceReservation], None] | None = None,
    ) -> PreemptiveResourceDemand:
        self._definition(resource_name)
        if self._request_exists(request_id):
            raise ValueError(f"preemptive resource request already exists: {request_id}")

        demand = PreemptiveResourceDemand(
            request_id=request_id,
            resource_name=resource_name,
            priority=priority,
            preempt=preempt,
            requested_at=requested_at,
            sequence=self._next_sequence(),
        )
        with self._persistence.transaction() as uow:
            uow.save_preemptive_resource_demand(demand)

        if on_acquired is not None:
            self._pending_callbacks[request_id] = on_acquired
        self._submit_demand(backend, demand)
        return demand

    def cancel_pending(self, backend, request_id: str) -> bool:
        demand = next(
            (
                demand
                for demand in self._persistence.preemptive_resource_demands()
                if demand.request_id == request_id
            ),
            None,
        )
        if demand is None:
            return False

        if not backend.cancel_preemptive_resource_request(request_id):
            return False

        with self._persistence.transaction() as uow:
            if uow.get_preemptive_resource_demand(request_id) != demand:
                raise RuntimeError(
                    f"preemptive resource demand changed during cancellation: {request_id}"
                )
            uow.delete_preemptive_resource_demand(request_id)

        self._pending_callbacks.pop(request_id, None)
        self._pending_grants.pop(request_id, None)
        self._pending_preemptions.pop(request_id, None)
        return True

    def release(self, backend, reservation_id: str) -> bool:
        reservation = next(
            (
                reservation
                for reservation in self._persistence.preemptive_resource_reservations()
                if reservation.reservation_id == reservation_id
            ),
            None,
        )
        if reservation is None:
            return False

        lease = self._backend_leases.get(reservation_id)
        if lease is None:
            raise RuntimeError(
                f"backend lease is not reconstructed for preemptive reservation: "
                f"{reservation_id}"
            )

        intent = PreemptiveResourceReleaseIntent(
            intent_id=deterministic_id("preemptive-resource-release", reservation_id),
            reservation_id=reservation_id,
            resource_name=reservation.resource_name,
            requested_at=getattr(backend, "now", reservation.acquired_at),
        )
        with self._persistence.transaction() as uow:
            if uow.get_preemptive_resource_reservation(reservation_id) != reservation:
                return False
            uow.save_preemptive_resource_release_intent(intent)

        backend.release_preemptive_resource(lease)
        self._finalize_release_intent(intent, reservation)
        self._backend_leases.pop(reservation_id, None)
        self._reconcile_pending_grants(reservation.resource_name)
        return True

    def _clear_ephemeral_state(self) -> None:
        """Discard all handles and handshake fragments owned by a dead backend."""

        self._backend_leases.clear()
        self._pending_grants.clear()
        self._pending_preemptions.clear()
        self._pending_callbacks.clear()

    def _submit_demand(self, backend, demand: PreemptiveResourceDemand) -> None:
        backend.request_preemptive_resource(
            demand.resource_name,
            request_id=demand.request_id,
            priority=demand.priority,
            preempt=demand.preempt,
            on_acquired=lambda lease, request_id=demand.request_id: (
                self._record_acquired(request_id=request_id, lease=lease)
            ),
            on_preempted=lambda event: self._record_preempted(event),
        )

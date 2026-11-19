"""Two-callback grant/preemption handshake of ``DurablePreemptiveResourceManager``.

Split from ``sose.core.preemption``.
"""

from __future__ import annotations

from datetime import datetime

from sose.backends.base import ResourceLease, ResourcePreemption
from sose.core.identity import deterministic_id
from sose.core.runtime import (
    PreemptiveResourceDemand,
    PreemptiveResourceReservation,
    ResourcePreemptionResult,
)


class _PreemptiveGrantHandshake:
    """Commit backend grants and preemptions into durable reservations."""

    def _record_acquired(self, *, request_id: str, lease: ResourceLease) -> None:
        existing = self.reservation_for(request_id)
        if existing is not None:
            self._backend_leases[existing.reservation_id] = lease
            return

        demand = next(
            (
                demand
                for demand in self._persistence.preemptive_resource_demands()
                if demand.request_id == request_id
            ),
            None,
        )
        if demand is None:
            return

        self._pending_grants[request_id] = lease
        if self._try_commit_preemption(request_id):
            return
        self._try_commit_normal_grant(request_id)

    def _record_preempted(self, event: ResourcePreemption) -> None:
        if not event.preempted_by:
            raise RuntimeError(
                f"preemption is missing preempting request identity: {event.request_id}"
            )
        displaced = next(
            (
                reservation
                for reservation in self._persistence.preemptive_resource_reservations()
                if reservation.request_id == event.request_id
            ),
            None,
        )
        if displaced is None:
            # Stale callback after an already-committed preemption.
            return
        if displaced.resource_name != event.resource_name:
            raise RuntimeError(
                f"preemption callback targets wrong resource: {event.request_id}"
            )

        self._pending_preemptions[event.preempted_by] = event
        self._try_commit_preemption(event.preempted_by)

    def _try_commit_preemption(self, preempting_request_id: str) -> bool:
        lease = self._pending_grants.get(preempting_request_id)
        event = self._pending_preemptions.get(preempting_request_id)
        if lease is None or event is None:
            return False

        demand = self._demand_for_request(preempting_request_id)
        displaced = self._reservation_for_request(event.request_id)
        if demand is None or displaced is None:
            return False
        if demand.resource_name != displaced.resource_name:
            raise RuntimeError(
                f"preemption pair crosses resources: {preempting_request_id}"
            )

        successor, result = self._build_preemption_outcome(
            demand=demand,
            displaced=displaced,
            preempted_at=event.preempted_at,
            acquired_at=lease.acquired_at,
        )

        with self._persistence.transaction() as uow:
            if uow.get_preemptive_resource_demand(demand.request_id) != demand:
                return False
            if (
                uow.get_preemptive_resource_reservation(displaced.reservation_id)
                != displaced
            ):
                return False
            uow.delete_preemptive_resource_demand(demand.request_id)
            uow.delete_preemptive_resource_reservation(displaced.reservation_id)
            uow.save_preemptive_resource_reservation(successor)
            uow.save_resource_preemption_result(result)

        self._backend_leases.pop(displaced.reservation_id, None)
        self._backend_leases[successor.reservation_id] = lease
        self._pending_grants.pop(preempting_request_id, None)
        self._pending_preemptions.pop(preempting_request_id, None)
        self._notify_acquired(successor)
        return True

    def _try_commit_normal_grant(self, request_id: str) -> bool:
        lease = self._pending_grants.get(request_id)
        demand = self._demand_for_request(request_id)
        if lease is None or demand is None:
            return False

        definition = self._definition(demand.resource_name)
        active = [
            reservation
            for reservation in self._persistence.preemptive_resource_reservations()
            if reservation.resource_name == demand.resource_name
        ]
        releasing = [
            intent
            for intent in self._persistence.preemptive_resource_release_intents()
            if intent.resource_name == demand.resource_name
        ]

        if releasing:
            return False
        if len(active) >= definition.capacity:
            # Acquisition while durable capacity is still full can only become
            # durable after the matching preemption callback arrives.
            return False

        reservation = self._commit_normal_grant(demand, lease)
        self._pending_grants.pop(request_id, None)
        self._notify_acquired(reservation)
        return True

    @staticmethod
    def _build_preemption_outcome(
        *,
        demand: PreemptiveResourceDemand,
        displaced: PreemptiveResourceReservation,
        preempted_at: datetime,
        acquired_at: datetime,
    ) -> tuple[PreemptiveResourceReservation, ResourcePreemptionResult]:
        successor = PreemptiveResourceReservation(
            reservation_id=deterministic_id(
                "preemptive-resource-reservation",
                demand.resource_name,
                demand.request_id,
            ),
            request_id=demand.request_id,
            resource_name=demand.resource_name,
            acquired_at=acquired_at,
            priority=demand.priority,
            sequence=demand.sequence,
        )
        result = ResourcePreemptionResult(
            result_id=deterministic_id(
                "resource-preemption",
                displaced.reservation_id,
                demand.request_id,
            ),
            resource_name=demand.resource_name,
            displaced_reservation_id=displaced.reservation_id,
            displaced_request_id=displaced.request_id,
            preempting_request_id=demand.request_id,
            successor_reservation_id=successor.reservation_id,
            preempted_at=preempted_at,
            sequence=demand.sequence,
        )
        return successor, result

    def _commit_normal_grant(
        self,
        demand: PreemptiveResourceDemand,
        lease: ResourceLease,
    ) -> PreemptiveResourceReservation:
        reservation = PreemptiveResourceReservation(
            reservation_id=deterministic_id(
                "preemptive-resource-reservation",
                demand.resource_name,
                demand.request_id,
            ),
            request_id=demand.request_id,
            resource_name=demand.resource_name,
            acquired_at=lease.acquired_at,
            priority=demand.priority,
            sequence=demand.sequence,
        )
        with self._persistence.transaction() as uow:
            if uow.get_preemptive_resource_demand(demand.request_id) != demand:
                existing = next(
                    (
                        current
                        for current in self._persistence.preemptive_resource_reservations()
                        if current.request_id == demand.request_id
                    ),
                    None,
                )
                if existing is not None:
                    return existing
                raise RuntimeError(
                    f"preemptive demand changed before grant: {demand.request_id}"
                )
            uow.delete_preemptive_resource_demand(demand.request_id)
            uow.save_preemptive_resource_reservation(reservation)

        self._backend_leases[reservation.reservation_id] = lease
        return reservation

    def _reconcile_pending_grants(self, resource_name: str) -> None:
        for request_id, lease in list(self._pending_grants.items()):
            if lease.resource_name != resource_name:
                continue
            if self._try_commit_preemption(request_id):
                continue
            self._try_commit_normal_grant(request_id)

    def _notify_acquired(self, reservation: PreemptiveResourceReservation) -> None:
        callback = self._pending_callbacks.pop(reservation.request_id, None)
        if callback is not None:
            callback(reservation)

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime

from sose.backends.base import ResourceLease, ResourcePreemption
from sose.core.identity import deterministic_id
from sose.core.runtime import (
    PreemptiveResourceDefinition,
    PreemptiveResourceDemand,
    PreemptiveResourceReleaseIntent,
    PreemptiveResourceReservation,
    ResourcePreemptionResult,
)
from sose.persistence.base import Persistence


class DurablePreemptiveResourceManager:
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

    def validate_rebuild(self) -> None:
        definitions = {
            definition.name: definition
            for definition in self._persistence.preemptive_resource_definitions()
        }
        reservations = self._persistence.preemptive_resource_reservations()
        demands = self._persistence.preemptive_resource_demands()
        releases = self._persistence.preemptive_resource_release_intents()

        for reservation in reservations:
            if reservation.resource_name not in definitions:
                raise RuntimeError(
                    f"preemptive reservation references unknown definition: "
                    f"{reservation.reservation_id}"
                )
        for demand in demands:
            if demand.resource_name not in definitions:
                raise RuntimeError(
                    f"preemptive demand references unknown definition: {demand.request_id}"
                )
        for intent in releases:
            reservation = next(
                (
                    reservation
                    for reservation in reservations
                    if reservation.reservation_id == intent.reservation_id
                ),
                None,
            )
            if reservation is None:
                raise RuntimeError(
                    f"preemptive release references unknown reservation: {intent.intent_id}"
                )
            if reservation.resource_name != intent.resource_name:
                raise RuntimeError(
                    f"preemptive release targets wrong resource: {intent.intent_id}"
                )

        for name, definition in definitions.items():
            active = sum(
                reservation.resource_name == name for reservation in reservations
            )
            if active > definition.capacity:
                raise RuntimeError(
                    f"preemptive resource {name} exceeds durable capacity"
                )

        demand_ids = {demand.request_id for demand in demands}
        reservation_ids = {reservation.request_id for reservation in reservations}
        overlap = demand_ids & reservation_ids
        if overlap:
            raise RuntimeError(
                f"preemptive requests are both pending and reserved: {sorted(overlap)!r}"
            )

        for result in self._persistence.resource_preemption_results():
            if result.resource_name not in definitions:
                raise RuntimeError(
                    f"preemption result references unknown definition: {result.result_id}"
                )

    def rebuild_backend(self, backend) -> int:
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

        # SimPy must materialize durable holders before pending higher-priority
        # demands are replayed, otherwise queue ordering can bypass preemption.
        run_until = getattr(backend, "run_until", None)
        if callable(run_until) and self._persistence.preemptive_resource_reservations():
            run_until(backend.now)

        for demand in self._persistence.preemptive_resource_demands():
            self._submit_demand(backend, demand)
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

    def _record_acquired(self, *, request_id: str, lease: ResourceLease) -> None:
        existing = next(
            (
                reservation
                for reservation in self._persistence.preemptive_resource_reservations()
                if reservation.request_id == request_id
            ),
            None,
        )
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

        demand = next(
            (
                demand
                for demand in self._persistence.preemptive_resource_demands()
                if demand.request_id == preempting_request_id
            ),
            None,
        )
        displaced = next(
            (
                reservation
                for reservation in self._persistence.preemptive_resource_reservations()
                if reservation.request_id == event.request_id
            ),
            None,
        )
        if demand is None or displaced is None:
            return False
        if demand.resource_name != displaced.resource_name:
            raise RuntimeError(
                f"preemption pair crosses resources: {preempting_request_id}"
            )

        successor = PreemptiveResourceReservation(
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
            preempted_at=event.preempted_at,
            sequence=demand.sequence,
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
        demand = next(
            (
                demand
                for demand in self._persistence.preemptive_resource_demands()
                if demand.request_id == request_id
            ),
            None,
        )
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

    def _finalize_release_intent(
        self,
        intent: PreemptiveResourceReleaseIntent,
        reservation: PreemptiveResourceReservation,
    ) -> None:
        with self._persistence.transaction() as uow:
            persisted_intent = uow.get_preemptive_resource_release_intent(
                intent.intent_id
            )
            persisted_reservation = uow.get_preemptive_resource_reservation(
                reservation.reservation_id
            )
            if persisted_intent != intent:
                raise RuntimeError(
                    f"preemptive release intent changed: {intent.intent_id}"
                )
            if persisted_reservation != reservation:
                raise RuntimeError(
                    f"preemptive reservation changed during release: "
                    f"{reservation.reservation_id}"
                )
            uow.delete_preemptive_resource_reservation(reservation.reservation_id)
            uow.delete_preemptive_resource_release_intent(intent.intent_id)

    def _finalize_interrupted_releases(self) -> None:
        for intent in self._persistence.preemptive_resource_release_intents():
            reservation = next(
                (
                    reservation
                    for reservation in self._persistence.preemptive_resource_reservations()
                    if reservation.reservation_id == intent.reservation_id
                ),
                None,
            )
            if reservation is None:
                with self._persistence.transaction() as uow:
                    uow.delete_preemptive_resource_release_intent(intent.intent_id)
                continue
            self._finalize_release_intent(intent, reservation)

    def _definition(self, name: str) -> PreemptiveResourceDefinition:
        definition = next(
            (
                definition
                for definition in self._persistence.preemptive_resource_definitions()
                if definition.name == name
            ),
            None,
        )
        if definition is None:
            raise KeyError(f"unknown preemptive resource definition: {name}")
        return definition

    def _request_exists(self, request_id: str) -> bool:
        return any(
            demand.request_id == request_id
            for demand in self._persistence.preemptive_resource_demands()
        ) or any(
            reservation.request_id == request_id
            for reservation in self._persistence.preemptive_resource_reservations()
        )

    def _next_sequence(self) -> int:
        return max(
            [
                *(
                    demand.sequence
                    for demand in self._persistence.preemptive_resource_demands()
                ),
                *(
                    reservation.sequence
                    for reservation in self._persistence.preemptive_resource_reservations()
                ),
                *(
                    result.sequence
                    for result in self._persistence.resource_preemption_results()
                ),
            ],
            default=0,
        ) + 1

"""Rebuild validation and release finalization of ``DurablePreemptiveResourceManager``.

Split from ``sose.core.preemption``.
"""

from __future__ import annotations

from sose.core.runtime import (
    PreemptiveResourceDefinition,
    PreemptiveResourceReleaseIntent,
    PreemptiveResourceReservation,
)


class _PreemptiveRebuildValidation:
    """Durable cross-record validation and interrupted-release finalization."""

    def validate_rebuild(self) -> None:
        definitions = {
            definition.name: definition
            for definition in self._persistence.preemptive_resource_definitions()
        }
        reservations = self._persistence.preemptive_resource_reservations()
        demands = self._persistence.preemptive_resource_demands()
        releases = self._persistence.preemptive_resource_release_intents()
        self._validate_preemptive_references(
            definitions,
            reservations=reservations,
            demands=demands,
            releases=releases,
        )
        self._validate_preemptive_capacities(definitions, reservations)
        self._validate_request_overlap(demands, reservations)
        self._validate_preemption_results(definitions)

    @staticmethod
    def _validate_preemptive_references(
        definitions: dict[str, PreemptiveResourceDefinition],
        *,
        reservations,
        demands,
        releases,
    ) -> None:
        
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

    @staticmethod
    def _validate_preemptive_capacities(
        definitions: dict[str, PreemptiveResourceDefinition],
        reservations,
    ) -> None:
        for name, definition in definitions.items():
            active = sum(
                reservation.resource_name == name for reservation in reservations
            )
            if active > definition.capacity:
                raise RuntimeError(
                    f"preemptive resource {name} exceeds durable capacity"
                )

    @staticmethod
    def _validate_request_overlap(demands, reservations) -> None:
        demand_ids = {demand.request_id for demand in demands}
        reservation_ids = {reservation.request_id for reservation in reservations}
        overlap = demand_ids & reservation_ids
        if overlap:
            raise RuntimeError(
                f"preemptive requests are both pending and reserved: {sorted(overlap)!r}"
            )

    def _validate_preemption_results(
        self,
        definitions: dict[str, PreemptiveResourceDefinition],
    ) -> None:
        for result in self._persistence.resource_preemption_results():
            if result.resource_name not in definitions:
                raise RuntimeError(
                    f"preemption result references unknown definition: {result.result_id}"
                )

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

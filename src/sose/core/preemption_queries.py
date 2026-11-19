"""Durable record lookups of ``DurablePreemptiveResourceManager``.

Split from ``sose.core.preemption``.
"""

from __future__ import annotations

from sose.core.runtime import (
    PreemptiveResourceDefinition,
    PreemptiveResourceDemand,
    PreemptiveResourceReservation,
)


class _PreemptiveDurableQueries:
    """Read-only lookups over durable preemptive-resource records."""

    def reservation_for(
        self,
        request_id: str,
    ) -> PreemptiveResourceReservation | None:
        """Return the durable reservation currently owned by a request, if any."""
        return next(
            (
                reservation
                for reservation in self._persistence.preemptive_resource_reservations()
                if reservation.request_id == request_id
            ),
            None,
        )

    def has_request(self, request_id: str) -> bool:
        """Return whether a preemptive request is durably pending or reserved."""
        return self._request_exists(request_id)

    def _demand_for_request(
        self,
        request_id: str,
    ) -> PreemptiveResourceDemand | None:
        return next(
            (
                demand
                for demand in self._persistence.preemptive_resource_demands()
                if demand.request_id == request_id
            ),
            None,
        )

    def _reservation_for_request(
        self,
        request_id: str,
    ) -> PreemptiveResourceReservation | None:
        return next(
            (
                reservation
                for reservation in self._persistence.preemptive_resource_reservations()
                if reservation.request_id == request_id
            ),
            None,
        )

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

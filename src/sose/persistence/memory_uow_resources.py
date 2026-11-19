"""Resource and preemptive-resource ledger methods of the in-memory unit of work.

Split from ``memory.py`` (``MemoryUnitOfWork``).
"""

from __future__ import annotations

from copy import deepcopy
from typing import TYPE_CHECKING

from sose.core.runtime import (
    PreemptiveResourceDefinition,
    PreemptiveResourceDemand,
    PreemptiveResourceReleaseIntent,
    PreemptiveResourceReservation,
    ResourceDefinition,
    ResourceDemand,
    ResourcePreemptionResult,
    ResourceReleaseIntent,
    ResourceReservation,
)

if TYPE_CHECKING:
    from .memory import _State


class ResourceRecordsUnitOfWorkMixin:
    """Staged reads/writes of resource and preemptive-resource ledger records."""

    _working: _State

    def get_resource_demand(self, request_id: str) -> ResourceDemand | None:
        value = self._working.resource_demands.get(request_id)
        return deepcopy(value) if value else None

    def resource_definitions(self) -> tuple[ResourceDefinition, ...]:
        """Read the same transaction-local definitions used to certify grants.

        An independent store-level read could race a concurrent PostgreSQL
        writer or omit definitions staged earlier in this UoW.
        """
        return tuple(
            deepcopy(self._working.resource_definitions[name])
            for name in sorted(self._working.resource_definitions)
        )

    def save_resource_definition(self, definition: ResourceDefinition) -> None:
        existing = self._working.resource_definitions.get(definition.name)
        if existing is not None and existing != definition:
            raise ValueError(f"resource definition already exists: {definition.name}")
        self._working.resource_definitions[definition.name] = deepcopy(definition)
        self._mark_dirty("resource_definitions", definition.name)

    def save_resource_demand(self, demand: ResourceDemand) -> None:
        if demand.resource_name not in self._working.resource_definitions:
            raise KeyError(f"unknown resource definition: {demand.resource_name}")
        if any(
            reservation.request_id == demand.request_id
            for reservation in self._working.resource_reservations.values()
        ):
            raise ValueError(f"resource request already reserved: {demand.request_id}")
        existing = self._working.resource_demands.get(demand.request_id)
        if existing is not None and existing != demand:
            raise ValueError(f"resource demand already exists: {demand.request_id}")
        self._working.resource_demands[demand.request_id] = deepcopy(demand)
        self._mark_dirty("resource_demands", demand.request_id)

    def delete_resource_demand(self, request_id: str) -> None:
        self._working.resource_demands.pop(request_id, None)
        self._mark_dirty("resource_demands", request_id)

    def get_resource_reservation(self, reservation_id: str) -> ResourceReservation | None:
        value = self._working.resource_reservations.get(reservation_id)
        return deepcopy(value) if value else None

    def resource_reservations(self) -> tuple[ResourceReservation, ...]:
        """Committed plus transaction-local reservations, never stale store state."""
        return tuple(
            deepcopy(self._working.resource_reservations[key])
            for key in sorted(self._working.resource_reservations)
        )

    def save_resource_reservation(self, reservation: ResourceReservation) -> None:
        if reservation.resource_name not in self._working.resource_definitions:
            raise KeyError(f"unknown resource definition: {reservation.resource_name}")
        if reservation.request_id in self._working.resource_demands:
            raise ValueError(f"resource request is still pending: {reservation.request_id}")
        if any(
            current.request_id == reservation.request_id
            and current.reservation_id != reservation.reservation_id
            for current in self._working.resource_reservations.values()
        ):
            raise ValueError(f"resource request already reserved: {reservation.request_id}")
        self._working.resource_reservations[reservation.reservation_id] = deepcopy(reservation)
        self._mark_dirty("resource_reservations", reservation.reservation_id)

    def delete_resource_reservation(self, reservation_id: str) -> None:
        self._working.resource_reservations.pop(reservation_id, None)
        self._mark_dirty("resource_reservations", reservation_id)

    def get_resource_release_intent(self, intent_id: str) -> ResourceReleaseIntent | None:
        value = self._working.resource_release_intents.get(intent_id)
        return deepcopy(value) if value else None

    def save_resource_release_intent(self, intent: ResourceReleaseIntent) -> None:
        if intent.reservation_id not in self._working.resource_reservations:
            raise KeyError(f"unknown resource reservation: {intent.reservation_id}")
        existing = self._working.resource_release_intents.get(intent.intent_id)
        if existing is not None and existing != intent:
            raise ValueError(f"resource release intent already exists: {intent.intent_id}")
        self._working.resource_release_intents[intent.intent_id] = deepcopy(intent)
        self._mark_dirty("resource_release_intents", intent.intent_id)

    def delete_resource_release_intent(self, intent_id: str) -> None:
        self._working.resource_release_intents.pop(intent_id, None)
        self._mark_dirty("resource_release_intents", intent_id)

    def get_preemptive_resource_demand(self, request_id: str) -> PreemptiveResourceDemand | None:
        value = self._working.preemptive_resource_demands.get(request_id)
        return deepcopy(value) if value else None

    def get_preemptive_resource_reservation(
        self, reservation_id: str
    ) -> PreemptiveResourceReservation | None:
        value = self._working.preemptive_resource_reservations.get(reservation_id)
        return deepcopy(value) if value else None

    def get_preemptive_resource_release_intent(
        self, intent_id: str
    ) -> PreemptiveResourceReleaseIntent | None:
        value = self._working.preemptive_resource_release_intents.get(intent_id)
        return deepcopy(value) if value else None

    def get_resource_preemption_result(self, result_id: str) -> ResourcePreemptionResult | None:
        value = self._working.resource_preemption_results.get(result_id)
        return deepcopy(value) if value else None

    def save_preemptive_resource_definition(self, definition: PreemptiveResourceDefinition) -> None:
        existing = self._working.preemptive_resource_definitions.get(definition.name)
        if existing is not None and existing != definition:
            raise ValueError(f"preemptive resource definition already exists: {definition.name}")
        self._working.preemptive_resource_definitions[definition.name] = deepcopy(definition)
        self._mark_dirty("preemptive_resource_definitions", definition.name)

    def save_preemptive_resource_demand(self, demand: PreemptiveResourceDemand) -> None:
        if demand.resource_name not in self._working.preemptive_resource_definitions:
            raise KeyError(f"unknown preemptive resource definition: {demand.resource_name}")
        if any(
            reservation.request_id == demand.request_id
            for reservation in self._working.preemptive_resource_reservations.values()
        ):
            raise ValueError(f"preemptive resource request already reserved: {demand.request_id}")
        existing = self._working.preemptive_resource_demands.get(demand.request_id)
        if existing is not None and existing != demand:
            raise ValueError(f"preemptive resource demand already exists: {demand.request_id}")
        self._working.preemptive_resource_demands[demand.request_id] = deepcopy(demand)
        self._mark_dirty("preemptive_resource_demands", demand.request_id)

    def delete_preemptive_resource_demand(self, request_id: str) -> None:
        self._working.preemptive_resource_demands.pop(request_id, None)
        self._mark_dirty("preemptive_resource_demands", request_id)

    def save_preemptive_resource_reservation(
        self, reservation: PreemptiveResourceReservation
    ) -> None:
        if reservation.resource_name not in self._working.preemptive_resource_definitions:
            raise KeyError(f"unknown preemptive resource definition: {reservation.resource_name}")
        if reservation.request_id in self._working.preemptive_resource_demands:
            raise ValueError(
                f"preemptive resource request is still pending: {reservation.request_id}"
            )
        if any(
            current.request_id == reservation.request_id
            and current.reservation_id != reservation.reservation_id
            for current in self._working.preemptive_resource_reservations.values()
        ):
            raise ValueError(
                f"preemptive resource request already reserved: {reservation.request_id}"
            )
        self._working.preemptive_resource_reservations[reservation.reservation_id] = deepcopy(
            reservation
        )
        self._mark_dirty(
            "preemptive_resource_reservations",
            reservation.reservation_id,
        )

    def delete_preemptive_resource_reservation(self, reservation_id: str) -> None:
        self._working.preemptive_resource_reservations.pop(reservation_id, None)
        self._mark_dirty("preemptive_resource_reservations", reservation_id)

    def save_preemptive_resource_release_intent(
        self, intent: PreemptiveResourceReleaseIntent
    ) -> None:
        if intent.reservation_id not in self._working.preemptive_resource_reservations:
            raise KeyError(f"unknown preemptive resource reservation: {intent.reservation_id}")
        existing = self._working.preemptive_resource_release_intents.get(intent.intent_id)
        if existing is not None and existing != intent:
            raise ValueError(
                f"preemptive resource release intent already exists: {intent.intent_id}"
            )
        self._working.preemptive_resource_release_intents[intent.intent_id] = deepcopy(intent)
        self._mark_dirty(
            "preemptive_resource_release_intents",
            intent.intent_id,
        )

    def delete_preemptive_resource_release_intent(self, intent_id: str) -> None:
        self._working.preemptive_resource_release_intents.pop(intent_id, None)
        self._mark_dirty("preemptive_resource_release_intents", intent_id)

    def save_resource_preemption_result(self, result: ResourcePreemptionResult) -> None:
        successor = self._working.preemptive_resource_reservations.get(
            result.successor_reservation_id
        )
        if successor is None:
            raise KeyError(
                f"unknown successor preemptive reservation: {result.successor_reservation_id}"
            )
        if successor.request_id != result.preempting_request_id:
            raise ValueError(
                f"preemption result successor does not match request: {result.result_id}"
            )
        if result.displaced_reservation_id in self._working.preemptive_resource_reservations:
            raise ValueError(
                f"displaced reservation is still active: {result.displaced_reservation_id}"
            )
        existing = self._working.resource_preemption_results.get(result.result_id)
        if existing is not None and existing != result:
            raise ValueError(f"resource preemption result already exists: {result.result_id}")
        self._working.resource_preemption_results[result.result_id] = deepcopy(result)
        self._mark_dirty("resource_preemption_results", result.result_id)

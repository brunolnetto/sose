"""Domain contracts for authoritative, finite, temporal resource allocations.

Intervals are UTC-aware, half-open [start, end). The persistence adapter is
responsible for atomic validation and durable event lineage.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from sose.core.resource_identity import ResourceAddress
from sose.core.identity import deterministic_id

ReservationStatus = Literal["reserved", "released", "preempted", "failed"]


def authoritative_effect_reservation_id(effect_id: str, *, scope: str | None = None) -> str:
    """Globally qualify locally deterministic effect IDs in a shared ledger.

    Scope is the authoritative operational namespace, not a worker identity.
    Existing single-store bookings retain their original IDs.
    """
    _nonempty(effect_id, "effect_id")
    if scope is None:
        return effect_id
    _nonempty(scope, "scope")
    return deterministic_id("federated-authoritative-resource-effect", scope, effect_id)


def _aware(value: datetime, name: str) -> None:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be a timezone-aware datetime")


def _nonempty(value: str, name: str) -> None:
    if not isinstance(value, str) or not value.strip() or value != value.strip():
        raise ValueError(f"{name} must be nonempty and trimmed")


@dataclass(frozen=True, slots=True)
class TemporalReservation:
    reservation_id: str
    address: ResourceAddress
    owner_id: str
    start_at: datetime
    end_at: datetime
    units: int = 1
    priority: int = 100
    causation_id: str | None = None
    status: ReservationStatus = "reserved"
    stopped_at: datetime | None = None

    def __post_init__(self) -> None:
        _nonempty(self.reservation_id, "reservation_id")
        _nonempty(self.owner_id, "owner_id")
        if not isinstance(self.address, ResourceAddress):
            raise TypeError("address must be ResourceAddress")
        _aware(self.start_at, "start_at")
        _aware(self.end_at, "end_at")
        if self.start_at >= self.end_at:
            raise ValueError("resource interval must have positive duration")
        if isinstance(self.units, bool) or not isinstance(self.units, int) or self.units < 1:
            raise ValueError("units must be a positive integer")
        if self.address.instance_id is not None and self.units != 1:
            raise ValueError("physical instances are indivisible")
        if isinstance(self.priority, bool) or not isinstance(self.priority, int):
            raise ValueError("priority must be an integer")
        if self.status not in ("reserved", "released", "preempted", "failed"):
            raise ValueError("unrecognized reservation status")
        if self.stopped_at is not None:
            _aware(self.stopped_at, "stopped_at")
            if not self.start_at <= self.stopped_at <= self.end_at:
                raise ValueError("stopped_at must lie inside the allocated interval")
        if self.status == "reserved" and self.stopped_at is not None:
            raise ValueError("active reservation cannot have stopped_at")
        if self.status != "reserved" and self.stopped_at is None:
            raise ValueError("terminal reservation must record stopped_at")

    @property
    def effective_end(self) -> datetime:
        return self.stopped_at if self.stopped_at is not None else self.end_at


@dataclass(frozen=True, slots=True)
class TemporalOutage:
    outage_id: str
    address: ResourceAddress
    start_at: datetime
    end_at: datetime
    recovered_at: datetime | None = None

    def __post_init__(self) -> None:
        _nonempty(self.outage_id, "outage_id")
        if not isinstance(self.address, ResourceAddress):
            raise TypeError("address must be ResourceAddress")
        _aware(self.start_at, "start_at")
        _aware(self.end_at, "end_at")
        if self.start_at >= self.end_at:
            raise ValueError("outage interval must have positive duration")
        if self.recovered_at is not None:
            _aware(self.recovered_at, "recovered_at")
            if not self.start_at <= self.recovered_at <= self.end_at:
                raise ValueError("recovered_at must lie inside the outage interval")

    @property
    def effective_end(self) -> datetime:
        return self.recovered_at if self.recovered_at is not None else self.end_at


class ResourceCapacityError(RuntimeError):
    """Requested interval cannot fit the authoritative finite resource supply."""


class ResourceConflictError(RuntimeError):
    """A duplicate identity was reused with incompatible business semantics."""

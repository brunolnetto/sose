"""Pure interval-feasibility rules of the PostgreSQL temporal resource ledger.

Split from ``postgres_resource_ledger.py`` (the bodies of ``_fits``, and the request
validation, victim selection and candidate enumeration of ``reserve``, ``fail`` and
``suggest_next_start``). Nothing here touches the database.
"""
from __future__ import annotations

from dataclasses import replace
from datetime import datetime
from typing import Callable, Iterable

from sose.core.resource_identity import ResourcePoolContract
from sose.core.resource_reservations import (
    ResourceCapacityError,
    ResourceConflictError,
    TemporalOutage,
    TemporalReservation,
    _aware,
)

Overlap = Callable[..., bool]
Fits = Callable[..., bool]


def overlap(a_start, a_end, b_start, b_end) -> bool:
    return a_start < b_end and b_start < a_end


def pool_lock_key(address) -> str:
    """Lock key of the pool that owns ``address`` (an instance maps to its pool)."""
    return (address.lock_key() if address.instance_id is None
            else replace(address, instance_id=None).lock_key())


def _outage_blocks(
    requested: TemporalReservation, outages: Iterable[TemporalOutage],
    overlaps: Overlap,
) -> bool:
    # A pool outage forbids all allocation; a named instance outage only
    # forbids that instance. Unavailability is evaluated over the interval.
    for outage in outages:
        # A recovery at outage.start_at is a legitimate zero-length
        # physical outage. Half-open intervals cannot overlap an empty
        # interval, even when the requested service spans that instant.
        if outage.effective_end <= outage.start_at:
            continue
        if (outage.address.instance_id is None or
            outage.address.instance_id == requested.address.instance_id):
            if overlaps(requested.start_at, requested.end_at,
                        outage.start_at, outage.effective_end):
                return True
    return False


def _competing(
    pool: ResourcePoolContract, requested: TemporalReservation,
    others: Iterable[TemporalReservation], overlaps: Overlap,
) -> list[TemporalReservation]:
    intervals = []
    for item in others:
        if item.effective_end <= item.start_at:
            continue
        if pool.instance_ids and item.address.instance_id != requested.address.instance_id:
            continue
        if overlaps(requested.start_at, requested.end_at,
                    item.start_at, item.effective_end):
            intervals.append(item)
    return intervals


def _within_capacity(
    pool: ResourcePoolContract, requested: TemporalReservation,
    intervals: list[TemporalReservation],
) -> bool:
    # Evaluate every occupancy change point, including another booking
    # starting midway through the new reservation (capacity > 1).
    points = {requested.start_at, requested.end_at}
    for item in intervals:
        points.add(max(requested.start_at, item.start_at))
        points.add(min(requested.end_at, item.effective_end))
    timeline = sorted(points)
    for at in timeline[:-1]:
        occupied = requested.units + sum(
            item.units for item in intervals
            if item.start_at <= at < item.effective_end
        )
        if occupied > pool.capacity:
            return False
    return True


def fits(
    pool: ResourcePoolContract, requested: TemporalReservation,
    others: Iterable[TemporalReservation], outages: Iterable[TemporalOutage],
    *, overlaps: Overlap,
) -> bool:
    if _outage_blocks(requested, outages, overlaps):
        return False
    intervals = _competing(pool, requested, others, overlaps)
    if pool.instance_ids:
        return not intervals
    return _within_capacity(pool, requested, intervals)


def check_reservation_pool(pool: ResourcePoolContract, request: TemporalReservation) -> None:
    """Reject a reservation whose address does not denote ``pool`` or a unit of it."""
    if pool.address() != replace(request.address, instance_id=None):
        raise ResourceConflictError("reservation pool identity mismatch")
    if pool.instance_ids and request.address.instance_id is None:
        raise ValueError("instanced resources must identify the physical instance")
    if not pool.instance_ids and request.address.instance_id is not None:
        raise ValueError("fungible resource pool cannot use physical instance")
    if request.address.instance_id is not None:
        pool.address(instance_id=request.address.instance_id)


def _preemptable(
    pool: ResourcePoolContract, request: TemporalReservation,
    b: TemporalReservation, overlaps: Overlap,
) -> bool:
    return (b.status == "reserved"
            and b.priority > request.priority
            and b.start_at <= request.start_at
            and overlaps(request.start_at, request.end_at,
                         b.start_at, b.effective_end)
            and (not pool.instance_ids or
                 b.address.instance_id == request.address.instance_id))


def select_victims(
    pool: ResourcePoolContract, request: TemporalReservation,
    bookings: list[TemporalReservation], outages: Iterable[TemporalOutage],
    *, fits_: Fits, overlaps: Overlap,
) -> list[TemporalReservation]:
    """Lower-priority bookings to preempt so that ``request`` fits, or raise."""
    victims = []
    # A higher integer means *lower* priority. Victims are selected
    # deterministically. Never preempt equal or higher priority.
    candidates = sorted(
        (b for b in bookings if _preemptable(pool, request, b, overlaps)),
        key=lambda b: (-b.priority, b.reservation_id),
    )
    tentative = list(bookings)
    for candidate in candidates:
        stop = max(candidate.start_at, request.start_at)
        proposal = replace(candidate, status="preempted", stopped_at=stop)
        tentative = [
            proposal if b.reservation_id == candidate.reservation_id else b
            for b in tentative
        ]
        victims.append(proposal)
        if fits_(pool, request, tentative, outages):
            break
    if not fits_(pool, request, tentative, outages):
        raise ResourceCapacityError(
            "no admissible lower-priority victims or instance unavailable"
        )
    return victims


def outage_interrupts(
    outage: TemporalOutage, item: TemporalReservation, overlaps: Overlap,
) -> bool:
    """Whether a new ``outage`` terminates the live booking ``item``."""
    if (outage.address.instance_id is not None and
        item.address.instance_id != outage.address.instance_id):
        return False
    if item.status != "reserved" or not overlaps(
        item.start_at, item.effective_end,
        outage.start_at, outage.end_at,
    ):
        return False
    return True


def check_advice_request(
    request: TemporalReservation, not_before: datetime, latest_start: datetime,
) -> None:
    if not isinstance(request, TemporalReservation) or request.status != "reserved":
        raise ValueError("prospective advice requires a new active TemporalReservation")
    _aware(not_before, "not_before")
    _aware(latest_start, "latest_start")
    if not_before < request.start_at:
        raise ValueError("prospective advice cannot start before original demand")
    if latest_start < not_before:
        raise ValueError("latest_start precedes not_before")


def check_advice_pool(pool: ResourcePoolContract, request: TemporalReservation) -> None:
    if pool.address() != replace(request.address, instance_id=None):
        raise ResourceConflictError("suggested physical pool identity mismatch")
    if pool.instance_ids:
        if request.address.instance_id is None:
            raise ValueError("instanced resource requires exact physical instance")
        pool.address(instance_id=request.address.instance_id)
    elif request.address.instance_id is not None:
        raise ValueError("fungible pool cannot request a physical instance")


def advice_candidates(
    pool: ResourcePoolContract, request: TemporalReservation,
    bookings: Iterable[TemporalReservation], outages: Iterable[TemporalOutage],
    *, not_before: datetime, latest_start: datetime,
) -> set[datetime]:
    """Start instants at which capacity can have improved inside the advice window."""
    candidates = {not_before}
    for booking in bookings:
        if (not pool.instance_ids
                or booking.address.instance_id == request.address.instance_id):
            if not_before < booking.effective_end <= latest_start:
                candidates.add(booking.effective_end)
    for outage in outages:
        if (outage.address.instance_id is None
                or outage.address.instance_id == request.address.instance_id):
            if not_before < outage.effective_end <= latest_start:
                candidates.add(outage.effective_end)
    return candidates

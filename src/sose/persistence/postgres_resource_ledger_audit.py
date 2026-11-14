"""Pure consistency checks behind ``PostgresTemporalResourceLedger.audit``.

Split from ``postgres_resource_ledger.py``. The ledger still issues every query and
decides the order of the checks; these functions only inspect rows already read.
"""
from __future__ import annotations

from typing import Callable, Iterable, Sequence

from sose.core.resource_identity import ResourcePoolContract
from sose.core.resource_reservations import (
    ResourceConflictError,
    TemporalOutage,
    TemporalReservation,
)


def check_event_causality(events: Sequence, ids: set) -> None:
    for event in events:
        if event[-1] is not None and event[-1] not in ids:
            raise ResourceConflictError("resource event has missing causal predecessor")


def _lacks_termination_event(booking: TemporalReservation, events: Sequence) -> bool:
    return booking.status in ("preempted", "failed") and not any(
        e[2] == booking.reservation_id and e[3] == booking.status
        for e in events
    )


def check_booking_events(
    bookings: Iterable[TemporalReservation], events: Sequence, ids: set,
) -> None:
    for booking in bookings:
        if f"reserve:{booking.reservation_id}" not in ids:
            raise ResourceConflictError("reservation lacks immutable allocation event")
        if booking.status == "released" and f"release:{booking.reservation_id}" not in ids:
            raise ResourceConflictError("release lacks durable causal event")
        if _lacks_termination_event(booking, events):
            raise ResourceConflictError("terminated reservation lacks causal event")


def check_outage_events(outages: Iterable[TemporalOutage], ids: set) -> None:
    for outage in outages:
        if f"outage:{outage.outage_id}" not in ids:
            raise ResourceConflictError("outage lacks immutable causal event")
        if outage.recovered_at is not None and f"recover:{outage.outage_id}" not in ids:
            raise ResourceConflictError("recovery lacks durable causal event")


def _check_occupancy(pool: ResourcePoolContract, live: list[TemporalReservation]) -> None:
    if pool.instance_ids:
        seen = [b.address.instance_id for b in live]
        if len(set(seen)) != len(seen):
            raise ResourceConflictError("same physical instance double-allocated")
    elif sum(b.units for b in live) > pool.capacity:
        raise ResourceConflictError("historical capacity overcommit")


def _check_outage_overlap(
    start, end, live: list[TemporalReservation], outages: list[TemporalOutage],
    overlaps: Callable[..., bool],
) -> None:
    for o in outages:
        # Recovery at the exact outage start makes [t,t)
        # empty. Both admission and historical audit must agree
        # that no positive-length booking can overlap it.
        if o.effective_end <= o.start_at:
            continue
        if not overlaps(start, end, o.start_at, o.effective_end):
            continue
        for b in live:
            if o.address.instance_id is None or (
                o.address.instance_id == b.address.instance_id
            ):
                raise ResourceConflictError("allocation overlaps resource outage")


def check_pool_history(
    pool: ResourcePoolContract, bookings: list[TemporalReservation],
    outages: list[TemporalOutage], *, overlaps: Callable[..., bool],
) -> None:
    """Reject overcommit, double allocation and allocation under outage of one pool."""
    # Compare the actual occupancy at every boundary; terminal
    # records still occupy the historical prefix until stopped_at.
    boundaries = sorted({
        boundary for b in bookings for boundary in (b.start_at, b.effective_end)
    })
    for start, end in zip(boundaries, boundaries[1:]):
        if start == end:
            continue
        live = [
            b for b in bookings if b.start_at <= start < b.effective_end
        ]
        _check_occupancy(pool, live)
        _check_outage_overlap(start, end, live, outages, overlaps)

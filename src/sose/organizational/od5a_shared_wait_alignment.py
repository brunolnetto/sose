"""Common prospective start across one source action's shared physical requests.

Read-only: no writes, grants or promises. Split out of od5a_shared_wait.
"""
from __future__ import annotations

from dataclasses import replace
from datetime import datetime

from sose.core.resource_reservations import ResourceConflictError, TemporalReservation

from .od5a_recurring_fence import _require_wall_clock


def _aligned_physical_start(
    ledger, requests: tuple[TemporalReservation, ...], *,
    not_before: datetime, latest_start: datetime,
) -> datetime | None:
    """Find the first *common* prospective boundary; no writes or promises.

    A max of per-resource earliest hints is not necessarily feasible: another
    instance may become occupied between those individual hints. Re-evaluate
    every request at the same increasing boundary, using the authoritative
    ledger's interval-end candidate search. Shared named instances cannot be
    double-claimed within one proposed source step. The read may race writers;
    the eventual transaction must repeat all checks and physically reserve.
    """
    _require_wall_clock(not_before)
    _require_wall_clock(latest_start)
    if latest_start < not_before:
        raise ValueError("latest_start precedes not_before")
    if not requests:
        raise ResourceConflictError("aligned physical advice requires requests")
    if not isinstance(requests[0], TemporalReservation):
        raise TypeError("aligned advice requires original temporal reservations")
    _require_one_source_action(requests, not_before)
    return _common_boundary(ledger, requests, not_before, latest_start)


def _require_one_source_action(
    requests: tuple[TemporalReservation, ...], not_before: datetime,
) -> None:
    # All requests must arise from ONE source action on ONE shared pool.
    # Otherwise a caller can compose individually valid hints from different
    # organizations, unrelated assets or distinct exogenous due boundaries
    # and misrepresent them as a jointly applicable, causally valid proposal.
    first = requests[0]
    addressed: set[str] = set()
    ids: set[str] = set()
    for request in requests:
        _require_same_source_action(request, first)
        # Distinct named physical instance is required for each simultaneous
        # request. Fungible-capacity groups need a separate joint-capacity
        # contract; pairwise availability does not prove pooled feasibility.
        if request.address.instance_id is None:
            raise ResourceConflictError("joint fungible availability is not certified")
        address = request.address.lock_key()
        if address in addressed or request.reservation_id in ids:
            raise ResourceConflictError("aligned advice aliases one physical instance or ID")
        addressed.add(address)
        ids.add(request.reservation_id)
        if not_before < request.start_at:
            raise ValueError("aligned advice cannot predate original physical request")


def _require_same_source_action(request: object, first: TemporalReservation) -> None:
    if not isinstance(request, TemporalReservation):
        raise TypeError("aligned advice requires original temporal reservations")
    if request.owner_id != first.owner_id:
        raise ResourceConflictError("aligned advice mixes unrelated source owners")
    if replace(request.address, instance_id=None) != replace(first.address, instance_id=None):
        raise ResourceConflictError("aligned advice mixes unrelated physical pools")
    if request.start_at != first.start_at:
        raise ResourceConflictError("aligned advice mixes different original action starts")
    if not isinstance(request.causation_id, str) or not request.causation_id:
        raise ResourceConflictError("aligned advice requires each original causal parent")


def _common_boundary(
    ledger, requests: tuple[TemporalReservation, ...],
    not_before: datetime, latest_start: datetime,
) -> datetime | None:
    candidate = not_before
    # Each successful iteration either terminates or advances to a strictly
    # later physical interval boundary, never polls by arbitrary time steps.
    # Fail closed on pathologically fragmented/churning ledgers.
    for _ in range(8192):
        next_candidates = []
        for request in requests:
            suggested = ledger.suggest_next_start(
                request, not_before=candidate, latest_start=latest_start,
            )
            if suggested is None:
                return None
            if suggested < candidate or suggested > latest_start:
                raise ResourceConflictError("ledger returned invalid prospective boundary")
            next_candidates.append(suggested)
        next_start = max(next_candidates)
        if next_start == candidate:
            return candidate
        candidate = next_start
    raise ResourceConflictError("aligned prospective advice exceeded bounded boundaries")

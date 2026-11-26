"""Admit an organization's physical pools against the authoritative ledger (#496, #477).

A ``PhysicalPoolRef(pool_id, owner_id, consumer_ids)`` is only names. It is admitted
when the PostgreSQL temporal resource ledger confirms it:

- ``owner_id`` names the pool's ledger scope. For an organization-scoped pool it is
  the pool's ``organization_id``, which must also be this organization. For a
  shared pool it is the literal ``"shared"``. Exactly one registered pool has
  ``pool_id`` within that scope; another tenant's pool with the same id does not
  interfere;
- every *active* (``reserved``) reservation in the pool by an owner of this
  organization belongs to a declared consumer component. Released, preempted or
  failed reservations are history, listed in the snapshot but not re-judged;
- an organization-scoped pool has no active reservation by an owner outside the
  organization;
- components have distinct owners, so a reservation names exactly one component.

``physical_ledger`` must come from ``read_physical_ledger``: admission trusts the
view it is given, so callers must not build one by hand.

The admitted pools then enter the organization snapshot. Each lists its contract and
this organization's reservations by component. In a shared pool, other owners'
active units are counted but not exposed.

The job-local SimPy resource queue (``ResourceDemand``/``ResourceReservation``) has no
owner and stays out of the snapshot.
"""
from __future__ import annotations

from dataclasses import dataclass, replace

from sose.core.resource_identity import ResourcePoolContract
from sose.core.resource_reservations import TemporalReservation

SHARED = "shared"


@dataclass(frozen=True, slots=True)
class PhysicalLedgerView:
    """One coherent read of the ledger's pools and reservations."""

    pools: tuple[ResourcePoolContract, ...]
    reservations: tuple[TemporalReservation, ...]


@dataclass(frozen=True, slots=True)
class PoolReservation:
    component_id: str
    owner_id: str
    reservation_id: str
    instance_id: str | None
    start_at: str
    end_at: str
    units: int
    status: str


@dataclass(frozen=True, slots=True)
class PoolSnapshot:
    pool_id: str
    resource_type: str
    scope: str
    organization_id: str | None
    capacity: int
    instance_ids: tuple[str, ...]
    consumer_ids: tuple[str, ...]
    reservations: tuple[PoolReservation, ...]
    # Active ("reserved") units held by owners outside this organization (shared pools).
    external_reserved_units: int


def read_physical_ledger(persistence) -> PhysicalLedgerView:
    """Read the ledger through ``persistence``'s connection (PostgreSQL only)."""
    from psycopg.errors import UndefinedTable

    ledger = persistence.temporal_resources(bootstrap=False)
    try:
        return PhysicalLedgerView(ledger.pools(), ledger.reservations())
    except UndefinedTable as error:            # no pool was ever registered here
        raise ValueError("physical pool references require a registered resource ledger") from error


def ledger_for(persistence, physical_ledger: bool, definition) -> PhysicalLedgerView | None:
    """The ledger view an admission caller passes on, when asked to read it.

    As in the snapshot, a definition without pools does not read the ledger. The
    read is its own transaction, before the caller's reads: a build, reattest or
    observation only needs admission once.
    """
    if not physical_ledger or not definition.physical_pools:
        return None
    if not callable(getattr(persistence, "temporal_resources", None)):
        raise TypeError("physical pools require the PostgreSQL resource ledger")
    return read_physical_ledger(persistence)


def require_admitted_pools(definition, ledger: PhysicalLedgerView) -> None:
    """Fail closed unless every declared pool is confirmed by the ledger."""
    owners = {component.component_id: component.owner_id for component in definition.components}
    organization_owners = set(owners.values())
    if len(organization_owners) != len(owners):
        raise ValueError("physical pools require components with distinct owner_id")
    for ref in definition.physical_pools:
        pool = _registered_pool(ref, ledger)
        _require_scope(ref, pool, definition.organization_id)
        consumers = {owners[component_id] for component_id in ref.consumer_ids}
        for reservation in _in_pool(pool, ledger):
            if reservation.status == "reserved":
                _require_active_owner(ref, pool, reservation.owner_id,
                                      organization_owners, consumers)


def _require_active_owner(ref, pool, owner, organization_owners, consumers) -> None:
    if owner in organization_owners and owner not in consumers:
        raise ValueError(
            f"owner {owner} reserved physical pool {ref.pool_id} "
            "without being a declared consumer"
        )
    if pool.scope == "organization" and owner not in organization_owners:
        raise ValueError(
            f"organization pool {ref.pool_id} has a reservation by foreign owner {owner}"
        )


def pool_snapshots(definition, ledger: PhysicalLedgerView) -> tuple[PoolSnapshot, ...]:
    """The admitted pools, as listed in the organization snapshot."""
    components = {component.owner_id: component.component_id
                  for component in definition.components}
    snapshots = []
    for ref in sorted(definition.physical_pools, key=lambda ref: ref.pool_id):
        pool = _registered_pool(ref, ledger)
        own, external = [], 0
        for reservation in _in_pool(pool, ledger):
            if reservation.owner_id in components:
                own.append(_record(reservation, components[reservation.owner_id]))
            elif reservation.status == "reserved":
                external += reservation.units
        snapshots.append(PoolSnapshot(
            pool_id=pool.pool_id, resource_type=pool.resource_type, scope=pool.scope,
            organization_id=pool.organization_id, capacity=pool.capacity,
            instance_ids=tuple(pool.instance_ids), consumer_ids=tuple(sorted(ref.consumer_ids)),
            reservations=tuple(sorted(own, key=lambda record: record.reservation_id)),
            external_reserved_units=external,
        ))
    return tuple(snapshots)


def _registered_pool(ref, ledger: PhysicalLedgerView) -> ResourcePoolContract:
    """The pool ``ref`` names, in the scope its ``owner_id`` implies."""
    matches = [pool for pool in ledger.pools
               if pool.pool_id == ref.pool_id and _in_scope(pool, ref.owner_id)]
    if not matches:
        raise ValueError(
            f"physical pool {ref.pool_id} is not registered in the resource ledger "
            f"in scope {ref.owner_id}"
        )
    if len(matches) > 1:
        raise ValueError(f"physical pool {ref.pool_id} is ambiguous in the resource ledger")
    return matches[0]


def _in_scope(pool: ResourcePoolContract, owner_id: str) -> bool:
    if owner_id == SHARED:
        return pool.scope == SHARED
    return pool.scope == "organization" and pool.organization_id == owner_id


def _require_scope(ref, pool: ResourcePoolContract, organization_id: str) -> None:
    """``_registered_pool`` already matched the scope ``owner_id`` names; an
    organization pool must also belong to *this* organization."""
    if pool.scope == "organization" and pool.organization_id != organization_id:
        raise ValueError(
            f"physical pool {ref.pool_id} belongs to organization {pool.organization_id}, "
            f"not {organization_id}"
        )


def _in_pool(pool: ResourcePoolContract, ledger: PhysicalLedgerView):
    address = pool.address()
    return [r for r in ledger.reservations if replace(r.address, instance_id=None) == address]


def _record(reservation: TemporalReservation, component_id: str) -> PoolReservation:
    return PoolReservation(
        component_id=component_id, owner_id=reservation.owner_id,
        reservation_id=reservation.reservation_id,
        instance_id=reservation.address.instance_id,
        start_at=reservation.start_at.isoformat(), end_at=reservation.end_at.isoformat(),
        units=reservation.units, status=reservation.status,
    )

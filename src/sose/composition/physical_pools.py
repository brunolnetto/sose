"""Admit an organization's physical pools against the authoritative ledger (#496, #477).

A ``PhysicalPoolRef(pool_id, owner_id, consumer_ids)`` is only names. It is admitted
when the PostgreSQL temporal resource ledger confirms it:

- exactly one registered pool has ``pool_id``;
- ``owner_id`` matches the pool's ledger scope. For an organization-scoped pool it
  is the pool's ``organization_id``, which must also be this organization. For a
  shared pool it is the literal ``"shared"``;
- every reservation in the pool by an owner of this organization belongs to a
  declared consumer component;
- an organization-scoped pool has no reservation by an owner outside the
  organization.

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
    ledger = persistence.temporal_resources(bootstrap=False)
    try:
        return PhysicalLedgerView(ledger.pools(), ledger.reservations())
    except Exception as error:  # the ledger tables do not exist yet
        raise ValueError("physical pool references require a registered resource ledger") from error


def require_admitted_pools(definition, ledger: PhysicalLedgerView) -> None:
    """Fail closed unless every declared pool is confirmed by the ledger."""
    owners = {component.component_id: component.owner_id for component in definition.components}
    organization_owners = set(owners.values())
    for ref in definition.physical_pools:
        pool = _registered_pool(ref, ledger)
        _require_scope(ref, pool, definition.organization_id)
        consumers = {owners[component_id] for component_id in ref.consumer_ids}
        for reservation in _in_pool(pool, ledger):
            owner = reservation.owner_id
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
    matches = [pool for pool in ledger.pools if pool.pool_id == ref.pool_id]
    if not matches:
        raise ValueError(f"physical pool {ref.pool_id} is not registered in the resource ledger")
    if len(matches) > 1:
        raise ValueError(f"physical pool {ref.pool_id} is ambiguous in the resource ledger")
    return matches[0]


def _require_scope(ref, pool: ResourcePoolContract, organization_id: str) -> None:
    if pool.scope == "organization":
        expected = pool.organization_id
        if expected != organization_id:
            raise ValueError(
                f"physical pool {ref.pool_id} belongs to organization {expected}, "
                f"not {organization_id}"
            )
    else:
        expected = SHARED
    if ref.owner_id != expected:
        raise ValueError(
            f"physical pool {ref.pool_id} owner {ref.owner_id} does not match its "
            f"ledger scope ({expected})"
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

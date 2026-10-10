"""Opt-in organizational resource identity and finite-capacity invariants.

These immutable contracts describe *which* finite resource is contested.
They do not acquire durable reservations or replace the existing SOSE
ResourceDefinition/ResourceReservation lifecycle.
"""
from __future__ import annotations

from dataclasses import dataclass
import json
from typing import Iterable, Literal

ResourceScope = Literal["organization", "shared"]


def _identifier(value: str, field: str) -> None:
    if not isinstance(value, str) or not value.strip() or value != value.strip():
        raise ValueError(f"{field} must be a nonempty, trimmed string")


@dataclass(frozen=True, slots=True)
class ResourceAddress:
    """Canonical, unambiguous identity of a pool or one physical instance."""

    resource_type: str
    pool_id: str
    scope: ResourceScope
    organization_id: str | None = None
    instance_id: str | None = None

    def __post_init__(self) -> None:
        _identifier(self.resource_type, "resource_type")
        _identifier(self.pool_id, "pool_id")
        if self.scope not in ("organization", "shared"):
            raise ValueError("resource scope must be organization or shared")
        if self.scope == "organization":
            _identifier(self.organization_id, "organization_id")
        elif self.organization_id is not None:
            raise ValueError("shared resources cannot have an owning organization")
        if self.instance_id is not None:
            _identifier(self.instance_id, "instance_id")

    def lock_key(self) -> str:
        """Stable, scope-separated advisory-lock name (not a durable lease).

        A JSON tuple prevents ambiguities from user-provided separators.
        The database's advisory-lock hash still has possible hash collisions.
        """
        return "sose:resource:v1:" + json.dumps(
            [
                self.scope, self.organization_id, self.resource_type,
                self.pool_id, self.instance_id,
            ],
            ensure_ascii=False, separators=(",", ":"),
        )


@dataclass(frozen=True, slots=True)
class ActiveResourceClaim:
    """An activity's requested occupied capacity at one instant."""

    claim_id: str
    address: ResourceAddress
    units: int = 1

    def __post_init__(self) -> None:
        _identifier(self.claim_id, "claim_id")
        if not isinstance(self.address, ResourceAddress):
            raise TypeError("address must be a ResourceAddress")
        if isinstance(self.units, bool) or not isinstance(self.units, int) or self.units < 1:
            raise ValueError("units must be a positive integer")
        if self.address.instance_id is not None and self.units != 1:
            raise ValueError("a named instance is indivisible and exclusive")


@dataclass(frozen=True, slots=True)
class ResourcePoolContract:
    """Finite pool: fungible integer units OR explicit exclusive instances."""

    resource_type: str
    pool_id: str
    scope: ResourceScope
    capacity: int
    organization_id: str | None = None
    instance_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        self.address()  # Validate scope, owner, type and pool identity.
        if isinstance(self.capacity, bool) or not isinstance(self.capacity, int) or self.capacity < 1:
            raise ValueError("capacity must be a positive integer")
        if not isinstance(self.instance_ids, tuple):
            raise TypeError("instance_ids must be an immutable tuple")
        for instance in self.instance_ids:
            _identifier(instance, "instance_id")
        if len(set(self.instance_ids)) != len(self.instance_ids):
            raise ValueError("resource instance identities must be unique")
        if self.instance_ids and len(self.instance_ids) != self.capacity:
            raise ValueError("enumerated instances must equal declared capacity")

    def address(self, *, instance_id: str | None = None) -> ResourceAddress:
        if instance_id is not None:
            if not self.instance_ids or instance_id not in self.instance_ids:
                raise ValueError("resource instance is not a member of this pool")
        return ResourceAddress(
            resource_type=self.resource_type,
            pool_id=self.pool_id,
            scope=self.scope,
            organization_id=self.organization_id,
            instance_id=instance_id,
        )

    def validate_active(self, claims: Iterable[ActiveResourceClaim]) -> None:
        """Reject overcommit or inconsistent resource binding at one instant.

        This pure check is not an atomic database allocation or a scheduler.
        Callers must bind the validated set to authoritative transactional state.
        """
        seen_claims: set[str] = set()
        used_instances: set[str] = set()
        used_units = 0
        pool_address = self.address()
        for claim in claims:
            if not isinstance(claim, ActiveResourceClaim):
                raise TypeError("claims must be ActiveResourceClaim records")
            if claim.claim_id in seen_claims:
                raise ValueError("duplicate resource claim_id")
            seen_claims.add(claim.claim_id)
            address = claim.address
            if (
                address.resource_type != pool_address.resource_type
                or address.pool_id != pool_address.pool_id
                or address.scope != pool_address.scope
                or address.organization_id != pool_address.organization_id
            ):
                raise ValueError("resource claim is for a different scoped pool")
            if self.instance_ids:
                if address.instance_id not in self.instance_ids:
                    raise ValueError("instance allocation must name a member of the pool")
                if address.instance_id in used_instances:
                    raise ValueError("exclusive instance is allocated twice")
                used_instances.add(address.instance_id)
            elif address.instance_id is not None:
                raise ValueError("fungible capacity cannot claim an instance")
            used_units += claim.units
            if used_units > self.capacity:
                raise ValueError("finite resource capacity exceeded")

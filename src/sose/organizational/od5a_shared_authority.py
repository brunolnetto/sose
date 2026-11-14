"""Opt-in shared PostgreSQL physical authority for native OD-5a source commits.

Split out of od5a_recurring, which re-exports every name.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from hashlib import sha256
from json import dumps

from sose.core.resource_identity import ResourcePoolContract
from sose.core.resource_reservations import (
    ResourceConflictError,
    TemporalReservation,
    authoritative_effect_reservation_id,
)
from sose.core.runtime import ResourceReservation
from sose.domain.entity import Entity
from sose.persistence.base import Persistence, UnitOfWork

from .od5a_durable import _time


@dataclass(frozen=True, slots=True)
class SharedPhysicalPoolBinding:
    """Immutable, explicitly named shared PG pool for one OD-5a organization.

    The source's local execution slot maps to one named *shared* instance.
    SimPy grants are ephemeral; the authoritative source UoW must also acquire
    the exact corresponding PostgreSQL temporal reservation before commit.
    """
    pool: ResourcePoolContract
    execution_instances: tuple[tuple[str, str], ...]
    authority_version: int = 1

    def __post_init__(self) -> None:
        if not isinstance(self.pool, ResourcePoolContract):
            raise TypeError("shared binding requires an immutable pool contract")
        if self.pool.scope != "shared" or not self.pool.instance_ids:
            raise ValueError("shared native execution requires enumerated shared instances")
        if type(self.authority_version) is not int or self.authority_version != 1:
            raise ValueError("unsupported shared physical authority version")
        if not isinstance(self.execution_instances, tuple) or not self.execution_instances:
            raise ValueError("shared binding needs explicit local-slot to instance mapping")
        self._validate_instance_mapping()

    def _validate_instance_mapping(self) -> None:
        slots: set[str] = set()
        instances: set[str] = set()
        for mapping in self.execution_instances:
            if not isinstance(mapping, tuple) or len(mapping) != 2:
                raise ValueError("shared instance mapping must be immutable (slot, instance)")
            slot, instance = mapping
            if not isinstance(slot, str) or not slot.startswith("execution:"):
                raise ValueError("only execution slots can use a shared execution pool")
            if slot in slots or instance in instances:
                raise ValueError("shared binding may not alias slots or physical instances")
            self.pool.address(instance_id=instance)
            slots.add(slot)
            instances.add(instance)

    def physical_instance(self, local_slot: str) -> str | None:
        return dict(self.execution_instances).get(local_slot)

    def fingerprint(self) -> str:
        return sha256(dumps(asdict(self), sort_keys=True, separators=(",", ":")).encode()).hexdigest()


class SharedPhysicalGrantAuthority:
    """Bind the existing temporal PG ledger to native SimPy source commits.

    This is an explicit opt-in; SQLite/local-only execution never pretends to
    possess a cross-organization physical permit. It holds only a *pool-scoped*
    transaction lock, with SOSE DomainEvent + job/lease checkpoint written in
    the same SQL transaction as temporal reservations and causal receipts.
    """

    def __init__(
        self, store: Persistence, binding: SharedPhysicalPoolBinding, owner_id: str,
        *, bootstrap_ledger: bool = True,
    ) -> None:
        from sose.persistence.postgres import PostgresPersistence

        if not isinstance(store, PostgresPersistence):
            raise TypeError("shared physical authority requires real PostgreSQL")
        self.binding = binding
        self.owner_id = owner_id
        self.ledger = store.temporal_resources(bootstrap=bootstrap_ledger)

    def guarded_pool(self):
        return self.ledger.guard_pool(self.binding.pool)

    def _slot(self, local_resource: str) -> tuple[str, str] | None:
        prefix = self.owner_id + "/"
        if not local_resource.startswith(prefix):
            raise ResourceConflictError("shared grant refers to another source owner")
        local_slot = local_resource[len(prefix):]
        physical = self.binding.physical_instance(local_slot)
        return (local_slot, physical) if physical is not None else None

    def _id(self, local_reservation_id: str) -> str:
        return authoritative_effect_reservation_id(
            local_reservation_id, scope=self.owner_id,
        )

    def apply(
        self, *, before: tuple[ResourceReservation, ...],
        releases: tuple[ResourceReservation, ...],
        grants: tuple[ResourceReservation, ...],
        after_state: dict, at: float, uow: UnitOfWork,
    ) -> None:
        """Verify and commit physical ownership inside the source's own UoW."""
        for local in before:
            self._verify_holding(local, at)

        for released in releases:
            if self._slot(released.resource_name) is None:
                continue
            self.ledger.release(self._id(released.reservation_id), at=_time(at))

        events = {item.event_id for item in uow.events()
                  if item.owner_id == self.owner_id}
        for local in grants:
            slot = self._slot(local.resource_name)
            if slot is None:
                continue
            self._reserve_grant(local, slot, after_state, events)

    def _verify_holding(self, local: ResourceReservation, at: float) -> None:
        slot = self._slot(local.resource_name)
        if slot is None:
            return
        instance = slot[1]
        expected = self.ledger.get(self._id(local.reservation_id))
        if (
            expected is None or expected.status != "reserved"
            or expected.owner_id != self.owner_id
            or expected.address != self.binding.pool.address(instance_id=instance)
            or expected.start_at != local.acquired_at
            or not expected.start_at <= _time(at) <= expected.end_at
        ):
            raise ResourceConflictError(
                "native source holding lacks matching fenced shared physical ledger"
            )

    def _reserve_grant(
        self, local: ResourceReservation, slot: tuple[str, str],
        after_state: dict, events: set[str],
    ) -> None:
        local_name, physical = slot
        origins = _granted_causes(after_state, local_name, local, events)
        ends = [
            action["at"] for action in after_state["actions"]
            if action.get("kind") == "execution_done"
            and action.get("resource") == local_name
        ]
        if len(ends) != 1 or _time(ends[0]) <= local.acquired_at:
            raise ResourceConflictError("shared native grant lacks positive exact service interval")
        external_id = self._id(local.reservation_id)
        if self.ledger.get(external_id) is not None:
            raise ResourceConflictError(
                "shared booking exists without a corresponding preflight local reservation"
            )
        self.ledger.reserve(TemporalReservation(
            reservation_id=external_id,
            address=self.binding.pool.address(instance_id=physical),
            owner_id=self.owner_id,
            start_at=local.acquired_at,
            end_at=_time(ends[0]),
            causation_id=origins[0],
        ))


def _granted_causes(
    after_state: dict, local_name: str, local: ResourceReservation, events: set[str],
) -> list[str]:
    activity = after_state["active"].get(local_name)
    if (
        activity is None or activity.get("reservation_id") != local.reservation_id
    ):
        raise ResourceConflictError("shared native grant missing active source work")
    origins = activity.get("causes")
    if (
        not isinstance(origins, list) or not origins
        or any(not isinstance(cause, str) or cause not in events
               for cause in origins)
    ):
        raise ResourceConflictError("shared grant has missing source causal parents")
    return origins


def _assert_shared_binding(
    source: Entity, binding: SharedPhysicalPoolBinding | None,
) -> None:
    persisted = source.attributes.get("shared_pool_binding")
    if binding is None:
        if persisted is not None:
            raise ValueError("shared-pool source requires its explicit immutable binding")
        return
    if not isinstance(binding, SharedPhysicalPoolBinding):
        raise TypeError("shared_pool_binding must be SharedPhysicalPoolBinding")
    if persisted != binding.fingerprint():
        raise ValueError("shared source binding differs from persisted physical authority")

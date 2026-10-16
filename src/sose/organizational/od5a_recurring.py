"""Per-organization recurring OD-5a source step driven by ephemeral native SimPy.

First bounded #477 native-grant slice: the authoritative reducer still
selects a deterministic free physical instance and owns causal transitions,
but a recurring source tick may NOT certify a new ResourceReservation unless
the ephemeral SimPy PriorityResource grants the exact same request/instance.
Preexisting occupancy is reconstructed from the durable ledger each tick.
This is NOT yet independent native pool scheduling, durable wait/fairness,
preemption, network/uncertain-COMMIT qualification or full OD-5a admission.

A scoped durable lease and SimulationJobState checkpoint fence a worker. The
physical event, resource ledger and job checkpoint commit in ONE transaction.
No SimPy Environment, Process, Event or callback is persisted.
"""
from __future__ import annotations

from contextlib import contextmanager
from copy import deepcopy
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from json import dumps
from typing import Callable, Literal

from sose.backends.simpy import SimPyBackend
from sose.core.identity import deterministic_id
from sose.core.runtime import ResourceDefinition, ResourceReservation
from sose.domain.entity import Entity
from sose.jobs.model import SimulationJobState
from sose.jobs.registration import job_registration_guard
from sose.persistence.base import Persistence, UnitOfWork

from .od5a_admission import OD5aCompiledArm
from .od5a_durable import (
    _KIND, _ORIGIN, _time, _advance_od5a_in_uow, _transition,
    initialize_od5a_durable,
)
from .od5a_mechanisms import OD5aWorkItem


_LEASE_KIND = "od5a_runner_lease"
_DOMAIN = "od5a"

@contextmanager
def _job_transaction(store: Persistence, job_id: str):
    """Serialize same-job PostgreSQL OLTP writes, not unrelated organizations.

    PostgresPersistence has last-writer-wins record updates. Its native
    business_resource_guard acquires an exclusive session advisory lock on
    precisely this organization's identity and releases it after the UoW.
    SQLite retains its own BEGIN IMMEDIATE transaction semantics.
    """
    guard = getattr(store, "business_resource_guard", None)
    if callable(guard):
        with guard("od5a:recurring:" + job_id):
            with store.transaction() as uow:
                yield uow
    else:
        with store.transaction() as uow:
            yield uow




class StaleOD5aRunnerClaimError(RuntimeError):
    """The worker cannot advance with a missing, expired or superseded epoch."""


@dataclass(frozen=True, slots=True)
class OD5aRunnerClaim:
    organization_id: str
    trigger_id: str
    owner_id: str
    epoch: int
    lease_expires_at: datetime


@dataclass(frozen=True, slots=True)
class OD5aRecurringStep:
    organization_id: str
    trigger_id: str
    action: Literal["advanced", "complete", "replayed"]
    epoch: int
    logical_time: datetime
    logical_tick: int


def _config(source: Entity) -> dict[str, str]:
    return {
        "pilot_identity": source.attributes["identity"],
        "model_spec_hash": source.attributes["model_spec_hash"],
        "arm_id": source.attributes["arm_id"],
    }


def _read_job(
    uow: UnitOfWork, job_id: str,
) -> tuple[Entity, SimulationJobState]:
    source = uow.get_entity(_KIND, job_id)
    job = uow.get_job_state(job_id)
    if source is None or job is None:
        raise KeyError(f"OD-5a recurring job is not initialized: {job_id}")
    identity = _config(source)
    if (
        job.domain_name != _DOMAIN
        or job.config_json != dumps(identity, sort_keys=True, separators=(",", ":"))
        or job.bootstrap_state != identity
        or not job.initialized
        or job.config_revision != 1
        or job.next_tick != source.attributes["step"]
        or type(job.run_count) is not int
        # The last real physical action marks the source done in the same
        # transaction that advances its step and the recurring run_count.
        # A subsequent completion query consumes no trigger or source step.
        or job.run_count != source.attributes["step"]
        # The reducer commits terminal source truth with the last real step.
        # A terminal physical organization can never hold a new active claim.
        # Enforce this in the common checkpoint reader (before any source
        # replay OR downstream boundary reconciliation).
        or (source.attributes["done"] and job.active_trigger_id is not None)
        # Bind the committed trigger identity to the authoritative run count.
        # Equality with a released lease alone is insufficient: forged job
        # and lease IDs could otherwise authorize an invalid replay.
        or job.last_completed_trigger_id != (
            deterministic_id("od5a-recurring-tick", job_id, job.run_count)
            if job.run_count else None
        )
        or job.logical_time != _time(source.attributes["clock"])
    ):
        raise ValueError("OD-5a recurring checkpoint contradicts authoritative organization")
    return source, job


def _require_wall_clock(now: datetime, lease_duration: timedelta | None = None) -> None:
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("OD-5a worker time must be timezone-aware")
    if lease_duration is not None and lease_duration <= timedelta(0):
        raise ValueError("OD-5a runner lease must be positive")


def _trusted_now(
    store: Persistence, wall_clock: Callable[[], datetime] | None,
) -> datetime:
    """Take an independent trusted timestamp AFTER obtaining the job lock.

    PostgreSQL ALWAYS uses its database server clock across contending hosts.
    The optional injected clock is permitted ONLY on non-PostgreSQL reference
    stores; callers cannot override a production database's fencing clock.
    Never allow caller-supplied 'now' to influence lease expiry.
    """
    db_clock = getattr(store, "trusted_utc_now", None)
    if callable(db_clock):
        # Production PostgreSQL fencing MUST use the authoritative server
        # clock, with no caller-controlled escape hatch. Even a private test
        # injection could permit a fast host to steal a live epoch.
        if wall_clock is not None:
            raise ValueError("PostgreSQL trusted lease time cannot be overridden")
        observed = db_clock()
    elif wall_clock is not None:
        # Test-only deterministic time is available on reference adapters.
        observed = wall_clock()
    else:
        observed = datetime.now(timezone.utc)
    _require_wall_clock(observed)
    return observed.astimezone(timezone.utc)


def _next_trigger_id(job: SimulationJobState) -> str:
    return deterministic_id("od5a-recurring-tick", job.job_id, job.run_count + 1)


def _lease_expiry(source: Entity) -> datetime:
    value = source.attributes.get("expires_at")
    if not isinstance(value, str):
        raise ValueError("OD-5a runner lease has invalid expiry representation")
    result = datetime.fromisoformat(value)
    _require_wall_clock(result)
    return result



def _assert_completed_released_lease(
    uow: UnitOfWork, job: SimulationJobState,
) -> None:
    """A completed step without its released fence cannot be safely replayed.

    A newer active trigger legitimately has a *claimed* lease instead;
    only idle/completed trigger history requires the released record.
    """
    if job.last_completed_trigger_id is None or job.active_trigger_id is not None:
        return
    if job.status != "ready" or job.phase != "idle":
        raise ValueError("OD-5a completed checkpoint lifecycle contradicts idle released lease")
    lease = uow.get_entity(_LEASE_KIND, job.job_id)
    if (
        lease is None
        or lease.state != "released"
        or lease.attributes.get("owner_id") is not None
        or lease.attributes.get("trigger_id") != job.last_completed_trigger_id
        or job.run_count < 1
        or lease.updated_at is None
        or not isinstance(lease.attributes.get("epoch"), int)
        or isinstance(lease.attributes.get("epoch"), bool)
        or lease.attributes["epoch"] < 1
        or lease.attributes["epoch"] < job.run_count
    ):
        raise ValueError("OD-5a completed trigger lacks released lease history")
    if _lease_expiry(lease) <= lease.updated_at:
        raise ValueError("OD-5a completed trigger lacks released lease history")



def _assert_active_claimed_lease(
    uow: UnitOfWork, job: SimulationJobState,
) -> None:
    """Prove claimed source fencing *history* before downstream mutations.

    A claimed worker may have died or its lease expired, yet independently
    committed physical events still need delivery. Therefore validate its
    immutable claim identity and lifecycle without requiring a live owner or
    extending the expired lease. Actual source execution still calls
    `_assert_claim` with the trusted PostgreSQL clock.
    """
    if job.active_trigger_id is None:
        return
    lease = uow.get_entity(_LEASE_KIND, job.job_id)
    if (
        job.status != "running"
        or job.phase != "advance"
        or job.active_trigger_id != _next_trigger_id(job)
        or lease is None
        or lease.state != "claimed"
        or lease.attributes.get("trigger_id") != job.active_trigger_id
        or not isinstance(lease.attributes.get("owner_id"), str)
        or not lease.attributes.get("owner_id")
        or lease.updated_at is None
        or not isinstance(lease.attributes.get("epoch"), int)
        or isinstance(lease.attributes.get("epoch"), bool)
        or lease.attributes["epoch"] < job.run_count + 1
    ):
        raise ValueError("OD-5a active lease contradicts runner claim history")
    try:
        expiry = _lease_expiry(lease)
    except (TypeError, ValueError) as exc:
        raise ValueError("OD-5a active lease contradicts runner claim expiry") from exc
    if expiry <= lease.updated_at:
        raise ValueError("OD-5a active lease contradicts runner claim expiry")


def _assert_claim(
    uow: UnitOfWork, claim: OD5aRunnerClaim, now: datetime,
) -> tuple[Entity, SimulationJobState, Entity]:
    source, job = _read_job(uow, claim.organization_id)
    lease = uow.get_entity(_LEASE_KIND, claim.organization_id)
    if lease is not None and lease.state == "claimed":
        stored_epoch = lease.attributes.get("epoch")
        if (
            not isinstance(stored_epoch, int)
            or isinstance(stored_epoch, bool)
            or stored_epoch < 1
        ):
            raise ValueError("invalid durable OD-5a runner epoch")
        if stored_epoch < job.run_count + 1:
            raise ValueError("OD-5a claimed lease epoch predates next run")
    if (
        lease is None
        or lease.state != "claimed"
        or lease.attributes.get("trigger_id") != claim.trigger_id
        or lease.updated_at is None
        or now < lease.updated_at
        or _lease_expiry(lease) <= lease.updated_at
        or job.active_trigger_id != claim.trigger_id
        or job.status != "running"
        or job.phase != "advance"
        or lease.attributes.get("owner_id") != claim.owner_id
        or lease.attributes.get("epoch") != claim.epoch
        or _lease_expiry(lease) != claim.lease_expires_at
        or now >= claim.lease_expires_at
    ):
        raise StaleOD5aRunnerClaimError(
            f"OD-5a runner claim superseded or expired: {claim.organization_id}"
        )
    return source, job, lease


def initialize_od5a_recurring_job(
    store: Persistence, *, job_id: str, arm: OD5aCompiledArm,
    items: tuple[OD5aWorkItem, ...],
) -> SimulationJobState:
    """Atomically commit an immutable source and its owning job checkpoint.

    A foreign SimulationJobState must not leave an orphaned pilot or resource
    definitions after failed registration. The binder runs inside the SAME
    authoritative source transaction, protected by the per-organization
    PostgreSQL guard when available.
    """
    registered: list[SimulationJobState] = []

    def bind(uow: UnitOfWork) -> None:
        registered.append(_register_od5a_job_in_uow(uow, job_id=job_id))

    # First claim the job identity across ALL domain creators; only then
    # enter the OD-5a physical/source fence. Fixed acquisition order prevents
    # the generic runner from racing a foreign job into this registration.
    with job_registration_guard(store, job_id):
        guard = getattr(store, "business_resource_guard", None)
        if callable(guard):
            with guard("od5a:recurring:" + job_id):
                initialize_od5a_durable(
                    store, job_id=job_id, arm=arm, items=items, _bind_uow=bind,
                )
        else:
            initialize_od5a_durable(
                store, job_id=job_id, arm=arm, items=items, _bind_uow=bind,
            )
    assert len(registered) == 1
    return registered[0]


def _register_od5a_job_in_uow(
    uow: UnitOfWork, *, job_id: str,
) -> SimulationJobState:
    # Shared transaction with physical source creation, no orphan on failure.
    source = uow.get_entity(_KIND, job_id)
    assert source is not None
    prior = uow.get_job_state(job_id)
    if prior is not None:
        if prior.domain_name != _DOMAIN:
            raise ValueError(
                "OD-5a organization is owned by another domain's SimulationJobState"
            )
        _read_job(uow, job_id)
        return prior
    if source.attributes["step"] != 0:
        raise ValueError("OD-5a recurring job cannot adopt untracked physical history")
    identity = _config(source)
    state = SimulationJobState(
        job_id=job_id,
        domain_name=_DOMAIN,
        config_json=dumps(identity, sort_keys=True, separators=(",", ":")),
        config_revision=1,
        status="ready",
        initialized=True,
        bootstrap_state=identity,
        logical_time=_time(source.attributes["clock"]),
        next_tick=source.attributes["step"],
    )
    uow.save_job_state(state)
    return state

def claim_od5a_recurring_tick(
    store: Persistence, *, job_id: str, owner_id: str,
    now: datetime, lease_duration: timedelta,
    trigger_id: str, recover: bool = False,
    _wall_clock: Callable[[], datetime] | None = None,
) -> OD5aRunnerClaim | None:
    """Claim the next exact source-step ID with a durable job-scoped epoch.

    A previously committed trigger is never admitted again; after a worker
    dies before source COMMIT, recovery must explicitly opt in and wait for
    the prior lease expiry.
    """
    _require_wall_clock(now, lease_duration)
    if not owner_id or not job_id or not trigger_id:
        raise ValueError("runner owner, organization and stable trigger ID must be nonempty")
    with _job_transaction(store, job_id) as uow:
        trusted_now = _trusted_now(store, _wall_clock)
        source, job = _read_job(uow, job_id)
        _assert_completed_released_lease(uow, job)
        if job.last_completed_trigger_id == trigger_id:
            return None
        expected_id = _next_trigger_id(job)
        if trigger_id != expected_id:
            raise ValueError("OD-5a runner trigger ID is not the next deterministic step")
        if source.attributes["done"]:
            if job.active_trigger_id is not None:
                raise ValueError("completed organization retains active runner trigger")
            return None
        if job.status == "paused":
            raise ValueError("OD-5a recurring job is paused")
        if job.active_trigger_id not in (None, trigger_id):
            raise StaleOD5aRunnerClaimError("OD-5a runner has a different unfinished trigger")

        existing = uow.get_entity(_LEASE_KIND, job_id)
        epoch = 1
        if existing is not None:
            epoch = existing.attributes.get("epoch", 0)
            if not isinstance(epoch, int) or isinstance(epoch, bool) or epoch < 1:
                raise ValueError("invalid durable OD-5a runner epoch")
            expiry = _lease_expiry(existing)
            previous_owner = existing.attributes.get("owner_id")
            if existing.state not in {"claimed", "released"}:
                raise ValueError("OD-5a runner lease has invalid durable lifecycle")
            if existing.state == "claimed" and (
                not isinstance(previous_owner, str) or not previous_owner
            ):
                raise ValueError("OD-5a runner lease is claimed without an owner (nonempty string required)")
            if previous_owner is not None and existing.state != "claimed":
                raise ValueError("OD-5a runner lease owner contradicts released state")
            # Validate the OLD physical fence before a recovering worker can
            # replace it. Otherwise an expired forged lease is laundered by
            # the new epoch, hiding its inconsistent causal trigger or clock.
            if existing.updated_at is None or expiry <= existing.updated_at:
                raise ValueError("OD-5a runner lease expiry contradicts claim timestamp")
            if existing.state == "claimed":
                if epoch < job.run_count + 1:
                    raise ValueError("OD-5a claimed lease epoch predates next run")
                if job.status != "running" or job.phase != "advance":
                    raise ValueError("OD-5a claimed lease contradicts job lifecycle")
                if (
                    job.active_trigger_id is None
                    or existing.attributes.get("trigger_id") != job.active_trigger_id
                ):
                    raise ValueError("OD-5a runner lease trigger contradicts active job")
            if existing.state == "released":
                # A released fence is created only when an authorized source
                # step and its job checkpoint commit together. A fabricated
                # pre-first-tick released epoch must never be laundered by
                # incrementing it into a new valid claim.
                if job.run_count < 1 or not job.last_completed_trigger_id:
                    raise ValueError("released lease requires completed trigger history")
                if job.active_trigger_id is not None:
                    raise ValueError("released OD-5a runner lease contradicts active job trigger")
                if existing.attributes.get("trigger_id") != job.last_completed_trigger_id:
                    raise ValueError("released OD-5a runner lease trigger contradicts completed job")
            if trusted_now < existing.updated_at:
                raise ValueError("trusted time precedes durable OD-5a runner lease timestamp")
            if previous_owner is not None and trusted_now < expiry:
                if (
                    previous_owner == owner_id
                    and job.active_trigger_id == trigger_id
                ):
                    return OD5aRunnerClaim(
                        job_id, trigger_id, owner_id, epoch, expiry,
                    )
                raise StaleOD5aRunnerClaimError("active OD-5a runner lease belongs to another worker")
            if job.active_trigger_id is not None and not recover:
                raise StaleOD5aRunnerClaimError("unfinished source trigger requires explicit recovery")
            epoch += 1
        elif job.active_trigger_id is not None:
            raise StaleOD5aRunnerClaimError("unfinished source trigger lost authoritative lease")

        expires_at = trusted_now + lease_duration
        lease = existing or Entity(
            id=job_id, entity_type=_LEASE_KIND, state="released",
        )
        lease.state = "claimed"
        lease.attributes = {
            "owner_id": owner_id,
            "epoch": epoch,
            "expires_at": expires_at.isoformat(),
            "trigger_id": trigger_id,
        }
        lease.touch(trusted_now)
        uow.save_entity(lease)
        uow.save_job_state(replace(
            job, status="running", phase="advance", active_trigger_id=trigger_id,
            last_triggered_at=trusted_now, last_error=None,
        ))
        return OD5aRunnerClaim(job_id, trigger_id, owner_id, epoch, expires_at)


class _PhysicalStepPreview:
    """Speculative reducer ledger: no authoritative mutations or SimPy stepping.

    The same reducer used by the committed runner predicts precisely which
    physical leases must be released and requested in the next action. This
    disposable view never writes source events or changes persisted state.
    """

    def __init__(self, reservations: tuple[ResourceReservation, ...]) -> None:
        self.reservations: dict[str, ResourceReservation] = {
            entry.reservation_id: entry for entry in reservations
        }
        self.releases: list[ResourceReservation] = []
        self.grants: list[ResourceReservation] = []

    def get_resource_reservation(self, reservation_id: str) -> ResourceReservation | None:
        return self.reservations.get(reservation_id)

    def resource_reservations(self) -> tuple[ResourceReservation, ...]:
        return tuple(self.reservations.values())

    def save_resource_reservation(self, reservation: ResourceReservation) -> None:
        if reservation.reservation_id in self.reservations:
            raise RuntimeError("speculative OD-5a duplicate physical reservation")
        self.reservations[reservation.reservation_id] = reservation
        self.grants.append(reservation)

    def delete_resource_reservation(self, reservation_id: str) -> None:
        try:
            released = self.reservations.pop(reservation_id)
        except KeyError as exc:
            raise RuntimeError("speculative OD-5a release has no durable lease") from exc
        self.releases.append(released)

    def append_event(self, event: object) -> None:
        # Event identity and causal links are verified by the real UoW, not
        # certified by this nonauthoritative physical scheduling preview.
        pass


class _RecurringNativePhysicalArbiter:
    """Native SimPy grant witness, reconstructed from a durable read snapshot.

    All SimPy event advancement occurs OUTSIDE persistence transactions.
    Inside the fenced UoW acquire/release only verify the already prepared
    physical identities; no foreign callback can execute under DB locks.
    """

    def __init__(
        self, backend: SimPyBackend, *,
        definitions: tuple[ResourceDefinition, ...],
        reservations: tuple[ResourceReservation, ...],
        resources: dict[str, str | None],
        job_id: str, due: float,
    ) -> None:
        self.backend = backend
        self.held: dict[str, tuple[str, str]] = {}
        self.expected: dict[str, str] = {}
        self.prepared_releases: dict[str, str] = {}
        if self.backend.now != _time(due):
            raise RuntimeError("OD-5a native recovery clock is not at source due boundary")
        definitions_by_name = {item.name: item for item in definitions}
        ledger_by_id = {item.reservation_id: item for item in reservations}
        for slot, reservation_id in resources.items():
            resource_name = f"{job_id}/{slot}"
            definition = definitions_by_name.get(resource_name)
            if definition is None or definition.capacity != 1:
                raise ValueError(
                    f"OD-5a native instance lacks authoritative unit capacity: {resource_name}"
                )
            self.backend.create_resource(resource_name, capacity=1)
            occupied = [r for r in reservations if r.resource_name == resource_name]
            if reservation_id is None:
                if occupied:
                    raise RuntimeError(f"untracked foreign physical occupancy: {resource_name}")
                continue
            saved = ledger_by_id.get(reservation_id)
            if saved is None or saved.resource_name != resource_name or len(occupied) != 1:
                raise RuntimeError(f"OD-5a physical lease contradicts ledger: {resource_name}")
            self.expected[resource_name] = reservation_id
            self._request(
                resource_name=resource_name, request_id=saved.request_id,
                reservation_id=reservation_id,
            )

    def _request(self, *, resource_name: str, request_id: str, reservation_id: str) -> None:
        def acquired(lease: object) -> None:
            if (
                getattr(lease, "resource_name") != resource_name
                or getattr(lease, "request_id") != request_id
                or self.backend.resource_snapshot(resource_name).in_use != 1
            ):
                raise RuntimeError("native SimPy lease identity or occupancy mismatch")
            if resource_name in self.held:
                raise RuntimeError("native SimPy allocated occupied physical instance")
            self.held[resource_name] = (
                reservation_id, getattr(lease, "lease_id"),
            )

        self.backend.request_resource(
            resource_name, request_id=request_id, on_acquired=acquired,
        )

    def prepare(
        self, *, releases: tuple[ResourceReservation, ...],
        grants: tuple[ResourceReservation, ...], at: float,
    ) -> None:
        """Process reconstructed holdings, release and next requests outside DB."""
        self.backend.run_until(_time(at))
        for resource_name, reservation_id in self.expected.items():
            current = self.held.get(resource_name)
            if current is None or current[0] != reservation_id:
                raise RuntimeError(f"native lease reconstruction failed: {resource_name}")

        for released in releases:
            resource_name = released.resource_name
            current = self.held.get(resource_name)
            if current is None or current[0] != released.reservation_id:
                raise RuntimeError(f"native physical release was not reconstructed: {resource_name}")
            self.backend.release_resource(current[1])
            del self.held[resource_name]
            self.prepared_releases[resource_name] = released.reservation_id

        for reservation in grants:
            if reservation.resource_name in self.held:
                raise RuntimeError(
                    f"native resource not released before new grant: {reservation.resource_name}"
                )
            self._request(
                resource_name=reservation.resource_name,
                request_id=reservation.request_id,
                reservation_id=reservation.reservation_id,
            )

        self.backend.run_until(_time(at))
        for reservation in grants:
            current = self.held.get(reservation.resource_name)
            if current is None or current[0] != reservation.reservation_id:
                raise RuntimeError(
                    f"native grant unavailable, durable wait not admitted: {reservation.resource_name}"
                )
            if self.backend.resource_snapshot(reservation.resource_name).in_use != 1:
                raise RuntimeError("native lease no longer occupies physical capacity")

    def acquire(self, *, resource_name: str, request_id: str, at: float) -> bool:
        # Fenced authoritative commit never advances SimPy or runs callbacks.
        if self.backend.now != _time(at):
            raise RuntimeError("native physical grant clock differs from source action")
        reservation_id = deterministic_id("od5a-durable-reservation", request_id)
        current = self.held.get(resource_name)
        return (
            current is not None
            and current[0] == reservation_id
            and self.backend.resource_snapshot(resource_name).in_use == 1
        )

    def release(self, *, resource_name: str, reservation_id: str) -> None:
        # The real reducer's release must exactly match a preflight release.
        # Actual native release has already run outside the SQLite/PG UoW.
        prepared = self.prepared_releases.pop(resource_name, None)
        if prepared != reservation_id:
            raise RuntimeError("authoritative release has no matching native completion")

    def verify_committed_step(self) -> None:
        if self.prepared_releases:
            raise RuntimeError("planned native releases were not consumed by source step")



def execute_od5a_claimed_tick(
    store: Persistence, *, claim: OD5aRunnerClaim, now: datetime,
    _wall_clock: Callable[[], datetime] | None = None,
) -> OD5aRecurringStep:
    """Use a fresh ephemeral SimPy dispatch to commit exactly one source step.

    A kill after claim or inside callback leaves the uncommitted action
    retriable with a higher epoch; a kill after COMMIT leaves the checkpoint
    and the physical event inseparable.
    """
    _require_wall_clock(now)
    with _job_transaction(store, claim.organization_id) as uow:
        source, _, _ = _assert_claim(
            uow, claim, _trusted_now(store, _wall_clock),
        )
        actions = source.attributes["actions"]
        due = (
            min(actions, key=lambda x: (x["at"], x["seq"]))["at"]
            if actions else source.attributes["clock"]
        )
        # Capture the COMPLETE committed action, not just at/seq/kind.
        # Otherwise a changed arrival item or completion resource can cross
        # the read-side preflight -> ephemeral SimPy dispatch boundary.
        next_action = (
            dict(min(actions, key=lambda x: (x["at"], x["seq"])))
            if actions else None
        )
    dispatched: list[OD5aRecurringStep] = []
    backend = SimPyBackend(origin=_ORIGIN)
    # A preflight hook or another worker may commit while SimPy advances
    # to the due boundary. Never hold a database write transaction during
    # this disposable, externally interceptable scheduling step.
    backend.run_until(_time(due))
    # Plan the reducer's upcoming leases using only a disposable ledger.
    # No SimPy callbacks execute while reading the transactional snapshot.
    with _job_transaction(store, claim.organization_id) as uow:
        source, _, _ = _assert_claim(
            uow, claim, _trusted_now(store, _wall_clock),
        )
        actions = source.attributes["actions"]
        actual = min(actions, key=lambda x: (x["at"], x["seq"])) if actions else None
        if actual != next_action:
            raise StaleOD5aRunnerClaimError(
                "OD-5a scheduled step changed before SimPy dispatch"
            )
        preflight_state = deepcopy(source.attributes)
        # Compare only this organization's physical addresses. An independent
        # owner can legitimately commit disjoint reservations between preflight
        # and this worker's fenced commit; no namespace-wide serialization.
        owned_names = {
            f"{claim.organization_id}/{slot}"
            for slot in preflight_state["resources"]
        }
        preflight_reservations = tuple(
            item for item in uow.resource_reservations()
            if item.resource_name in owned_names
        )
        preview = _PhysicalStepPreview(preflight_reservations)
        _transition(
            preview, deepcopy(preflight_state),
            claim.organization_id,
        )
        native = _RecurringNativePhysicalArbiter(
            backend, definitions=uow.resource_definitions(),
            reservations=preflight_reservations,
            resources=preflight_state["resources"],
            job_id=claim.organization_id, due=due,
        )

    # Native acquisitions and their potentially adversarial callbacks are
    # processed with NO SQLite BEGIN IMMEDIATE or PostgreSQL owner guard held.
    native.prepare(
        releases=tuple(preview.releases), grants=tuple(preview.grants), at=due,
    )

    def commit_source_step() -> None:
        with _job_transaction(store, claim.organization_id) as uow:
            # Re-read the trusted clock AFTER the lock. Neither invocation
            # time nor the worker host's clock can extend/stale the fence.
            commit_at = _trusted_now(store, _wall_clock)
            source, job, lease = _assert_claim(uow, claim, commit_at)
            actions = source.attributes["actions"]
            actual = min(actions, key=lambda x: (x["at"], x["seq"])) if actions else None
            if actual != next_action or source.attributes != preflight_state:
                raise StaleOD5aRunnerClaimError(
                    "OD-5a source changed across native physical acquisition"
                )
            current_owned = tuple(
                item for item in uow.resource_reservations()
                if item.resource_name in owned_names
            )
            if current_owned != preflight_reservations:
                raise StaleOD5aRunnerClaimError(
                    "OD-5a own physical ledger changed before native commit"
                )
            advanced = _advance_od5a_in_uow(
                uow, job_id=claim.organization_id, native=native,
            )
            native.verify_committed_step()
            committed = uow.get_entity(_KIND, claim.organization_id)
            assert committed is not None
            job = replace(
                job, status="ready", phase="idle", active_trigger_id=None,
                last_completed_trigger_id=claim.trigger_id,
                logical_time=_time(committed.attributes["clock"]),
                next_tick=committed.attributes["step"],
                run_count=job.run_count + 1,
                last_error=None,
            )
            uow.save_job_state(job)
            lease.state = "released"
            lease.attributes = {**lease.attributes, "owner_id": None}
            lease.touch(commit_at)
            uow.save_entity(lease)
            dispatched.append(OD5aRecurringStep(
                claim.organization_id, claim.trigger_id,
                "advanced" if advanced else "complete",
                claim.epoch, job.logical_time, job.next_tick,
            ))

    backend.schedule_at(
        _time(due), commit_source_step,
        key=("od5a-recurring", claim.organization_id, claim.trigger_id, claim.epoch),
    )
    backend.run_until(_time(due))
    if len(dispatched) != 1:
        raise RuntimeError("native SimPy did not dispatch one authoritative OD-5a source action")
    return dispatched[0]


def run_od5a_recurring_tick(
    store: Persistence, *, job_id: str, owner_id: str,
    now: datetime, lease_duration: timedelta,
    trigger_id: str, recover: bool = False,
    _wall_clock: Callable[[], datetime] | None = None,
) -> OD5aRecurringStep:
    """One bounded recurring source step; ACK/certification are separate jobs.

    The external scheduler MUST persist and reuse trigger_id on retry; silently
    deriving it after a lost commit response would skip replay and execute
    another physical source step.
    """
    _require_wall_clock(now, lease_duration)
    if not trigger_id:
        raise ValueError("a stable external recurring trigger ID is required")
    with _job_transaction(store, job_id) as uow:
        source, job = _read_job(uow, job_id)
        _assert_completed_released_lease(uow, job)
        if trigger_id is not None and trigger_id == job.last_completed_trigger_id:
            return OD5aRecurringStep(
                job_id, trigger_id, "replayed", 0,
                job.logical_time, job.next_tick,
            )
        if source.attributes["done"] and job.active_trigger_id is None:
            return OD5aRecurringStep(
                job_id, trigger_id,
                "complete", 0, job.logical_time, job.next_tick,
            )
    claim = claim_od5a_recurring_tick(
        store, job_id=job_id, owner_id=owner_id, now=now,
        lease_duration=lease_duration, trigger_id=trigger_id, recover=recover,
        _wall_clock=_wall_clock,
    )
    if claim is None:
        with _job_transaction(store, job_id) as uow:
            _, job = _read_job(uow, job_id)
            return OD5aRecurringStep(
                job_id, trigger_id,
                "replayed", 0, job.logical_time, job.next_tick,
            )
    return execute_od5a_claimed_tick(
        store, claim=claim, now=now, _wall_clock=_wall_clock,
    )


__all__ = [
    "OD5aRunnerClaim", "OD5aRecurringStep", "StaleOD5aRunnerClaimError",
    "initialize_od5a_recurring_job", "claim_od5a_recurring_tick",
    "execute_od5a_claimed_tick", "run_od5a_recurring_tick",
]

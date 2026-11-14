"""Transactional OD-5a operational recovery oracle on SOSE EnginePersistence.

Small hand-configured workloads only. Persistent entities own outstanding
semantic actions and per-job clocks; ResourceReservation owns physical grants;
DomainEvent owns causal history. The in-memory Python scheduler is disposable.

This is NOT the confirmatory OD-5a execution engine or independent freeze.
"""
from __future__ import annotations

from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from hashlib import sha256
import json
from typing import Any, Callable, Protocol

from sose.core.events import DomainEvent
from sose.core.identity import deterministic_id
from sose.core.runtime import ResourceDefinition, ResourceReservation
from sose.domain.entity import Entity
from sose.persistence.base import Persistence

from .od5a_admission import OD5aCompiledArm
from .od5a_durable_inputs import _precise_time, _time_us, validated_od5a_pilot
from .od5a_mechanisms import OD5aWorkItem


_ORIGIN = datetime(2000, 1, 1, tzinfo=timezone.utc)
_KIND = "od5a_durable_pilot"


class _NativePhysicalArbiter(Protocol):
    """Ephemeral native acquisition fence used only by the recurring adapter."""

    def acquire(self, *, resource_name: str, request_id: str, at: float) -> bool: ...
    def release(self, *, resource_name: str, reservation_id: str) -> None: ...



def _add_exact_seconds(start: float, duration: float) -> float:
    """Arithmetic in integer microseconds, never binary-float addition."""
    ticks = _time_us(start, "action start") + _time_us(duration, "action duration")
    if abs(ticks) > 2**53:
        raise ValueError("scheduled completion exceeds lossless microsecond offset range")
    seconds = ticks / 1_000_000
    if _time_us(seconds, "scheduled completion") != ticks:
        raise ValueError(
            "scheduled completion cannot round-trip through float seconds "
            "without losing a microsecond"
        )
    return seconds


def _time(offset: float) -> datetime:
    return _ORIGIN + timedelta(microseconds=_time_us(offset, "event timestamp"))


def _key(item_id: str, station: str) -> str:
    return deterministic_id("od5a-durable-stage", item_id, station)


def _event_id(job_id: str, kind: str, item_id: str, source: str = "", target: str = "") -> str:
    return deterministic_id("od5a-durable-event", job_id, kind, item_id, source, target)


def _canonical_hash(payload: object) -> str:
    return sha256(json.dumps(
        payload, sort_keys=True, separators=(",", ":"), allow_nan=False,
    ).encode("utf-8")).hexdigest()


def _request(state: dict[str, Any], kind: str, request: dict[str, Any]) -> None:
    state[f"pending_{kind}"].append(request)


def _schedule(state: dict[str, Any], *, at: float, kind: str, **data: object) -> None:
    seq = state["next_seq"]
    state["next_seq"] += 1
    state["actions"].append({"at": at, "seq": seq, "kind": kind, **data})


def _emit(
    uow: Any, state: dict[str, Any], *, job_id: str, kind: str,
    item_id: str, at: float, source: str = "", target: str = "",
    start: float | None = None, resource: str | None = None,
    causes: list[str] | None = None,
) -> str:
    eid = _event_id(job_id, kind, item_id, source, target)
    parents = sorted(causes or [])
    event = DomainEvent(
        owner_id=job_id,
        event_id=eid,
        name=f"od5a.{kind}.v1",
        entity_type=_KIND,
        entity_id=job_id,
        occurred_at=_time(at),
        tick=state["step"],
        causation_id=parents[-1] if parents else None,
        correlation_id=job_id,
        payload={
            "kind": kind, "item_id": item_id,
            "source": source, "target": target,
            "start": at if start is None else start, "end": at,
            "resource": resource, "causes": parents,
            "config_epoch": 0, "model_spec_hash": state["model_spec_hash"],
        },
    )
    uow.append_event(event)
    return eid


def _grant(
    uow: Any, state: dict[str, Any], *,
    job_id: str, resource: str, at: float,
    kind: str, request: dict[str, Any],
    native: _NativePhysicalArbiter | None = None,
) -> bool:
    if resource in state["active"]:
        raise RuntimeError(f"double allocation of physical resource {resource}")
    if state["resources"][resource] not in (None, ""):
        raise RuntimeError("conflicting authoritative physical reservation")
    resource_name = f"{job_id}/{resource}"
    # The local entity only records *our* leases. Another participant may
    # already own this physical instance under an unrelated reservation ID.
    # Read the transaction-local authoritative ledger before granting.
    if any(
        lease.resource_name == resource_name
        for lease in uow.resource_reservations()
    ):
        raise RuntimeError(
            f"authoritative physical resource capacity exhausted: {resource_name}"
        )
    request_id = deterministic_id(
        "od5a-durable-request", job_id, kind, request["item"],
        request["source"], request["target"],
    )
    reservation_id = deterministic_id("od5a-durable-reservation", request_id)
    if uow.get_resource_reservation(reservation_id) is not None:
        raise RuntimeError(f"duplicate durable reservation for {request_id}")
    duration = (
        state["services"][request["item"]][request["target"]]
        if kind == "execution" else state["coordination_latency"]
    )
    # The recurrent adapter cannot persist capacity on a reducer-only grant.
    # The native backend grants the *exact* resource and request identity;
    # the lease remains live until the caller's authoritative UoW commits.
    if native is not None and not native.acquire(
        resource_name=resource_name, request_id=request_id, at=at,
    ):
        # A blocked native request must never certify an arrival or stage
        # completion that has no acquired physical slot. Until an explicit
        # durable waiting/retry protocol exists, fail closed and roll back
        # the entire fenced source action rather than lose its causal parent.
        raise RuntimeError(
            f"native physical capacity blocked, durable wait not yet admitted: "
            f"{resource_name}"
        )
    uow.save_resource_reservation(ResourceReservation(
        reservation_id=reservation_id,
        request_id=request_id,
        resource_name=resource_name,
        acquired_at=_time(at),
        sequence=state["next_seq"],
    ))
    state["resources"][resource] = reservation_id
    state["active"][resource] = {
        **request, "kind": kind, "start": at,
        "reservation_id": reservation_id,
    }
    _schedule(
        state, at=_add_exact_seconds(at, duration), kind=f"{kind}_done",
        resource=resource,
    )
    return True


def _dispatch(
    uow: Any, state: dict[str, Any], *,
    job_id: str, at: float, native: _NativePhysicalArbiter | None = None,
) -> None:
    for resource in state["resources"]:
        if not resource.startswith("execution:") or state["resources"][resource] is not None:
            continue
        if state["pending_execution"]:
            request = state["pending_execution"].pop(0)
            if not _grant(
                uow, state, job_id=job_id, resource=resource,
                at=at, kind="execution", request=request, native=native,
            ):
                state["pending_execution"].insert(0, request)
    if state["resources"]["coordination:0"] is None and state["pending_coordination"]:
        request = state["pending_coordination"].pop(0)
        if not _grant(
            uow, state, job_id=job_id, resource="coordination:0",
            at=at, kind="coordination", request=request, native=native,
        ):
            state["pending_coordination"].insert(0, request)


def _queue_stage(
    state: dict[str, Any], *, item_id: str, target: str, causes: list[str],
) -> None:
    identity = _key(item_id, target)
    if identity in state["requested"]:
        raise RuntimeError("stage requested twice")
    state["requested"].append(identity)
    _request(state, "execution", {
        "item": item_id, "source": target, "target": target, "causes": causes,
    })


def _transition(
    uow: Any, state: dict[str, Any], job_id: str,
    native: _NativePhysicalArbiter | None = None,
) -> bool:
    if not state["actions"]:
        return _finish_idle(state)
    action = min(state["actions"], key=lambda x: (x["at"], x["seq"]))
    state["actions"].remove(action)
    now = action["at"]
    if now < state["clock"]:
        raise RuntimeError("organizational clock regressed")
    state["clock"] = now
    state["step"] += 1
    kind = action["kind"]
    if kind == "arrival":
        _on_arrival(uow, state, job_id, action["item"], now)
    elif kind in {"execution_done", "coordination_done"}:
        _on_lease_done(uow, state, job_id, kind, action["resource"], now, native)
    else:
        raise RuntimeError(f"unknown scheduled durable action: {kind}")
    _dispatch(uow, state, job_id=job_id, at=now, native=native)
    if not state["actions"] and len(state["completed"]) == len(state["services"]):
        if _has_physical_work(state):
            raise RuntimeError("completed world retains unresolved physical work")
        state["done"] = True
    return True


def _has_physical_work(state: dict[str, Any]) -> bool:
    return bool(state["active"] or state["pending_execution"] or state["pending_coordination"])


def _finish_idle(state: dict[str, Any]) -> bool:
    if _has_physical_work(state):
        raise RuntimeError("pending physical allocations but no future completion action")
    if len(state["completed"]) != len(state["services"]):
        raise RuntimeError("durable dependency graph stopped without completing every item")
    state["done"] = True
    return False


def _on_arrival(uow: Any, state: dict[str, Any], job_id: str, item: str, now: float) -> None:
    cause = _emit(
        uow, state, job_id=job_id, kind="arrival", item_id=item, at=now,
    )
    for root in state["stations"]:
        if not state["incoming"][root]:
            _queue_stage(state, item_id=item, target=root, causes=[cause])


def _release_lease(
    uow: Any, state: dict[str, Any], job_id: str, kind: str, resource: str,
    native: _NativePhysicalArbiter | None,
) -> dict[str, Any]:
    allocation = state["active"].pop(resource, None)
    if allocation is None or allocation["kind"] + "_done" != kind:
        raise RuntimeError("durable resource action has no matching lease")
    reservation_id = allocation["reservation_id"]
    if state["resources"][resource] != reservation_id:
        raise RuntimeError("physical resource truth differs from active allocation")
    if uow.get_resource_reservation(reservation_id) is None:
        raise RuntimeError("physical lease disappeared before completion")
    if native is not None:
        native.release(
            resource_name=f"{job_id}/{resource}",
            reservation_id=reservation_id,
        )
    uow.delete_resource_reservation(reservation_id)
    state["resources"][resource] = None
    return allocation


def _on_lease_done(
    uow: Any, state: dict[str, Any], job_id: str, kind: str, resource: str,
    now: float, native: _NativePhysicalArbiter | None,
) -> None:
    allocation = _release_lease(uow, state, job_id, kind, resource, native)
    item = allocation["item"]
    source, target = allocation["source"], allocation["target"]
    category = "execution" if kind == "execution_done" else "coordination"
    emitted = _emit(
        uow, state, job_id=job_id, kind=category, item_id=item, at=now,
        source=source, target=target, start=allocation["start"],
        resource=resource, causes=allocation["causes"],
    )
    if kind == "execution_done":
        _on_stage_executed(state, item, target, emitted, now)
    else:
        _on_dependency_delivered(state, item, source, target, emitted)


def _on_stage_executed(
    state: dict[str, Any], item: str, target: str, emitted: str, now: float,
) -> None:
    state["finished"].append(_key(item, target))
    if all(_key(item, node) in state["finished"] for node in state["stations"]):
        state["completed"][item] = now
    for downstream in sorted(state["outgoing"][target]):
        _request(state, "coordination", {
            "item": item, "source": target,
            "target": downstream, "causes": [emitted],
        })


def _on_dependency_delivered(
    state: dict[str, Any], item: str, source: str, target: str, emitted: str,
) -> None:
    satisfied = state["received"].setdefault(_key(item, target), {})
    if source in satisfied:
        raise RuntimeError("duplicate delivered dependency")
    satisfied[source] = emitted
    if set(satisfied) == set(state["incoming"][target]):
        _queue_stage(
            state, item_id=item, target=target,
            causes=list(satisfied.values()),
        )


@contextmanager
def _source_transaction(store: Persistence, job_id: str):
    """Serialize source ownership transfer and standalone advances per job.

    PostgresPersistence uses last-writer-wins record updates. Its existing
    per-resource advisory session lock serializes the single organization
    through the transaction; separate organizations never share this lock.
    The recurring runner uses the EXACT SAME lock key before binding the
    SimulationJobState or advancing physical source truth.
    """
    guard = getattr(store, "business_resource_guard", None)
    if callable(guard):
        with guard("od5a:recurring:" + job_id):
            with store.transaction() as uow:
                yield uow
    else:
        with store.transaction() as uow:
            yield uow


def initialize_od5a_durable(
    store: Persistence, *, job_id: str, arm: OD5aCompiledArm,
    items: tuple[OD5aWorkItem, ...],
    _bind_uow: Callable[[Any], None] | None = None,
) -> None:
    """Idempotent physical start; optional owner checkpoint binds in SAME UoW.

    The recurring adapter uses this private binder to atomically enroll its
    SimulationJobState. Standalone reference sources have no owner binder.
    """
    pilot = validated_od5a_pilot(job_id=job_id, arm=arm, items=items)
    stations, incoming, outgoing = pilot.stations, pilot.incoming, pilot.outgoing
    raw_slots, coordination_latency = pilot.execution_slots, pilot.coordination_latency
    transition = pilot.transition
    identity = _pilot_identity(arm, items)
    resources = {f"execution:{i}": None for i in range(raw_slots)}
    resources["coordination:0"] = None
    with _source_transaction(store, job_id) as uow:
        previous = uow.get_entity(_KIND, job_id)
        if previous is not None:
            if previous.attributes["identity"] != identity:
                raise ValueError("immutable OD-5a pilot workload/model identity conflicts")
            if _bind_uow is not None:
                _bind_uow(uow)
            return
        for resource in resources:
            uow.save_resource_definition(ResourceDefinition(f"{job_id}/{resource}", 1))
        state: dict[str, Any] = {
            "identity": identity, "arm_id": arm.arm_id,
            "model_spec_hash": arm.model.model_spec_hash,
            "stations": list(stations),
            "incoming": incoming, "outgoing": outgoing,
            "services": {item.item_id: dict(item.service_by_station) for item in items},
            "coordination_latency": coordination_latency,
            "clock": 0.0, "step": 0, "next_seq": 0,
            "resources": resources, "active": {},
            "actions": [], "pending_execution": [], "pending_coordination": [],
            "requested": [], "finished": [], "received": {},
            "completed": {}, "done": False,
        }
        for item in items:
            _schedule(
                state, at=max(float(item.arrival_at), transition),
                kind="arrival", item=item.item_id,
            )
        uow.save_entity(Entity(
            id=job_id, entity_type=_KIND, state="running",
            created_at=_ORIGIN, updated_at=_ORIGIN,
            attributes=state,
        ))
        if _bind_uow is not None:
            _bind_uow(uow)


def _pilot_identity(arm: OD5aCompiledArm, items: tuple[OD5aWorkItem, ...]) -> str:
    return _canonical_hash({
        "admission_hash": arm.admission_hash,
        "workload": [
            {"item": item.item_id, "at": item.arrival_at,
             "service": [list(pair) for pair in item.service_by_station]}
            for item in items
        ],
        "pilot_version": 1,
    })


def _advance_od5a_in_uow(
    uow: Any, *, job_id: str, native: _NativePhysicalArbiter | None = None,
) -> bool:
    """Advance physical truth inside a caller-owned authoritative UoW.

    The recurring adapter couples its fenced job checkpoint and the physical
    event/resource transaction with this same transaction, never a second
    post-commit write. The standalone reference reducer calls it unchanged.
    """
    entity = uow.get_entity(_KIND, job_id)
    if entity is None:
        raise KeyError(f"unknown OD-5a durable pilot: {job_id}")
    state = deepcopy(entity.attributes)
    if state["done"]:
        return False
    advanced = _transition(uow, state, job_id, native=native)
    entity.attributes = state
    entity.state = "done" if state["done"] else "running"
    entity.touch(_time(state["clock"]))
    uow.save_entity(entity)
    return advanced


def advance_od5a_durable(store: Persistence, *, job_id: str) -> bool:
    """One atomic reference step, unless a fenced recurring job owns the source.

    The source reducer remains the independent reference oracle, but may not
    mutate a source owned by a recurring SimulationJobState without that job's
    lease and checkpoint committing in the same UoW. The recurring adapter
    uses _advance_od5a_in_uow after validating its epoch inside the UoW.
    """
    with _source_transaction(store, job_id) as uow:
        owning_job = uow.get_job_state(job_id)
        if owning_job is not None and owning_job.domain_name == "od5a":
            raise RuntimeError(
                "OD-5a recurring source requires fenced job advancement"
            )
        return _advance_od5a_in_uow(uow, job_id=job_id)


def od5a_durable_state(store: Persistence, *, job_id: str) -> dict[str, Any]:
    entity = store.entity(_KIND, job_id)
    if entity is None:
        raise KeyError(job_id)
    return deepcopy(entity.attributes)


def od5a_durable_events(store: Persistence, *, job_id: str) -> tuple[DomainEvent, ...]:
    return tuple(sorted(
        (e for e in store.events() if e.entity_type == _KIND and e.entity_id == job_id),
        key=lambda e: (e.tick if e.tick is not None else -1, e.event_id),
    ))

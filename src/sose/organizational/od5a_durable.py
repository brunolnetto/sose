"""Transactional OD-5a operational recovery oracle on SOSE EnginePersistence.

Small hand-configured workloads only. Persistent entities own outstanding
semantic actions and per-job clocks; ResourceReservation owns physical grants;
DomainEvent owns causal history. The in-memory Python scheduler is disposable.

This is NOT the confirmatory OD-5a execution engine or independent freeze.
"""
from __future__ import annotations

from contextlib import contextmanager
from copy import deepcopy
from decimal import Decimal
from datetime import datetime, timedelta, timezone
from hashlib import sha256
from math import isfinite
import json
from typing import Any, Callable

from sose.core.events import DomainEvent
from sose.core.identity import deterministic_id
from sose.core.runtime import ResourceDefinition, ResourceReservation
from sose.domain.entity import Entity
from sose.persistence.base import Persistence

from .od5a_admission import OD5aCompiledArm, ALL_ARMS
from .od5a_mechanisms import OD5aWorkItem


_ORIGIN = datetime(2000, 1, 1, tzinfo=timezone.utc)
_KIND = "od5a_durable_pilot"


def _time_us(value: float | int, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not isfinite(value):
        raise ValueError(f"{label} requires a finite numeric time")
    ticks = Decimal(str(value)) * 1_000_000
    if ticks != ticks.to_integral_value():
        raise ValueError(f"{label} must have exact microsecond resolution")
    # A float second offset cannot uniquely represent every microsecond beyond
    # this bound; reject rather than alias two distinct semantic timestamps.
    if abs(ticks) > 2**53:
        raise ValueError(f"{label} exceeds lossless microsecond offset range")
    return int(ticks)


def _precise_time(value: float | int, label: str) -> None:
    _time_us(value, label)


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
) -> None:
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


def _dispatch(uow: Any, state: dict[str, Any], *, job_id: str, at: float) -> None:
    for resource in state["resources"]:
        if not resource.startswith("execution:") or state["resources"][resource] is not None:
            continue
        if state["pending_execution"]:
            request = state["pending_execution"].pop(0)
            _grant(uow, state, job_id=job_id, resource=resource,
                   at=at, kind="execution", request=request)
    if state["resources"]["coordination:0"] is None and state["pending_coordination"]:
        request = state["pending_coordination"].pop(0)
        _grant(uow, state, job_id=job_id, resource="coordination:0",
               at=at, kind="coordination", request=request)


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


def _transition(uow: Any, state: dict[str, Any], job_id: str) -> bool:
    if not state["actions"]:
        if not state["active"] and not state["pending_execution"] and not state["pending_coordination"]:
            if len(state["completed"]) != len(state["services"]):
                raise RuntimeError("durable dependency graph stopped without completing every item")
            state["done"] = True
            return False
        raise RuntimeError("pending physical allocations but no future completion action")
    action = min(state["actions"], key=lambda x: (x["at"], x["seq"]))
    state["actions"].remove(action)
    now = action["at"]
    if now < state["clock"]:
        raise RuntimeError("organizational clock regressed")
    state["clock"] = now
    state["step"] += 1
    kind = action["kind"]

    if kind == "arrival":
        item = action["item"]
        cause = _emit(
            uow, state, job_id=job_id, kind="arrival", item_id=item, at=now,
        )
        for root in state["stations"]:
            if not state["incoming"][root]:
                _queue_stage(state, item_id=item, target=root, causes=[cause])
    elif kind in {"execution_done", "coordination_done"}:
        resource = action["resource"]
        allocation = state["active"].pop(resource, None)
        if allocation is None or allocation["kind"] + "_done" != kind:
            raise RuntimeError("durable resource action has no matching lease")
        reservation_id = allocation["reservation_id"]
        if state["resources"][resource] != reservation_id:
            raise RuntimeError("physical resource truth differs from active allocation")
        if uow.get_resource_reservation(reservation_id) is None:
            raise RuntimeError("physical lease disappeared before completion")
        uow.delete_resource_reservation(reservation_id)
        state["resources"][resource] = None
        item = allocation["item"]
        source, target = allocation["source"], allocation["target"]
        category = "execution" if kind == "execution_done" else "coordination"
        emitted = _emit(
            uow, state, job_id=job_id, kind=category, item_id=item, at=now,
            source=source, target=target, start=allocation["start"],
            resource=resource, causes=allocation["causes"],
        )
        if kind == "execution_done":
            state["finished"].append(_key(item, target))
            if all(_key(item, node) in state["finished"] for node in state["stations"]):
                state["completed"][item] = now
            for downstream in sorted(state["outgoing"][target]):
                _request(state, "coordination", {
                    "item": item, "source": target,
                    "target": downstream, "causes": [emitted],
                })
        else:
            satisfied = state["received"].setdefault(_key(item, target), {})
            if source in satisfied:
                raise RuntimeError("duplicate delivered dependency")
            satisfied[source] = emitted
            if set(satisfied) == set(state["incoming"][target]):
                _queue_stage(
                    state, item_id=item, target=target,
                    causes=list(satisfied.values()),
                )
    else:
        raise RuntimeError(f"unknown scheduled durable action: {kind}")
    _dispatch(uow, state, job_id=job_id, at=now)
    if not state["actions"] and len(state["completed"]) == len(state["services"]):
        if state["active"] or state["pending_execution"] or state["pending_coordination"]:
            raise RuntimeError("completed world retains unresolved physical work")
        state["done"] = True
    return True



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
    if not job_id or arm.arm_id not in ALL_ARMS or not items:
        raise ValueError("registered arm, job ID and nonempty workload required")
    # The arm label alone is not scientific admission. Treating hand-made arm
    # objects as compiled would permit unpaid treatment or mislabeled baseline.
    if not arm.compiler_admitted:
        raise ValueError("OD-5a arm lacks compiler admission or mutated admission hash")
    if arm.arm_id == "baseline":
        if arm.charged_cost is not None or arm.intervention is not None:
            raise ValueError("compiled baseline cannot carry an intervention or cost")
    elif (
        arm.charged_cost is None or arm.intervention is None
        or arm.intervention.intervention_id != arm.arm_id
    ):
        raise ValueError("compiled treatment must carry matching intervention and cost")
    stations = tuple(sorted(arm.model.stations))
    if not stations:
        raise ValueError("at least one physical station required")
    if len({x.item_id for x in items}) != len(items):
        raise ValueError("duplicate work items")
    if any({station for station, _ in x.service_by_station} != set(stations)
           or len(x.service_by_station) != len(stations) for x in items):
        raise ValueError("each item must provide each station's exogenous service demand")
    edges = tuple(tuple(edge) for edge in arm.model.routing["dependency_edges"])
    incoming = {s: [] for s in stations}
    outgoing = {s: [] for s in stations}
    for source, target in edges:
        if source not in outgoing or target not in incoming or source == target:
            raise ValueError("unknown dependency edge station")
        outgoing[source].append(target)
        incoming[target].append(source)
    raw_slots = arm.model.parameters["execution_slots"]
    if isinstance(raw_slots, bool) or not isinstance(raw_slots, int) or raw_slots < 1:
        raise ValueError("physical slots must be positive integers")
    coordination_latency = float(arm.model.parameters["coordination_latency"])
    transition = 0.0 if arm.charged_cost is None else arm.charged_cost.transition_time
    if coordination_latency < 0 or transition < 0:
        raise ValueError("pilot coordination and transition intervals must be nonnegative")
    _precise_time(coordination_latency, "coordination latency")
    _precise_time(transition, "transition time")
    for item in items:
        _precise_time(item.arrival_at, "arrival time")
        for _, duration in item.service_by_station:
            _precise_time(duration, "service duration")
    workload = [
        {"item": item.item_id, "at": item.arrival_at,
         "service": [list(pair) for pair in item.service_by_station]}
        for item in items
    ]
    identity = _canonical_hash({
        "admission_hash": arm.admission_hash,
        "workload": workload,
        "pilot_version": 1,
    })
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


def _advance_od5a_in_uow(uow: Any, *, job_id: str) -> bool:
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
    advanced = _transition(uow, state, job_id)
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

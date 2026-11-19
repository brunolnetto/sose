"""OD-5a durable reducer primitives and state handlers (split from od5a_durable)."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from hashlib import sha256
import json
from typing import Any, Protocol

from sose.core.identity import deterministic_id
from sose.core.runtime import ResourceReservation

from .od5a_durable_inputs import _time_us


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


def _has_physical_work(state: dict[str, Any]) -> bool:
    return bool(state["active"] or state["pending_execution"] or state["pending_coordination"])


def _finish_idle(state: dict[str, Any]) -> bool:
    if _has_physical_work(state):
        raise RuntimeError("pending physical allocations but no future completion action")
    if len(state["completed"]) != len(state["services"]):
        raise RuntimeError("durable dependency graph stopped without completing every item")
    state["done"] = True
    return False


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

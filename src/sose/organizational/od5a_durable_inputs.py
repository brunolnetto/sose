"""Admission checks and exact time arithmetic for the OD-5a durable pilot.

Pure validation only: no persistence, scheduling or event emission.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from math import isfinite

from .od5a_admission import ALL_ARMS, OD5aCompiledArm
from .od5a_mechanisms import OD5aWorkItem


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


@dataclass(frozen=True, slots=True)
class ValidatedOD5aPilot:
    stations: tuple[str, ...]
    incoming: dict[str, list[str]]
    outgoing: dict[str, list[str]]
    execution_slots: int
    coordination_latency: float
    transition: float


def validated_od5a_pilot(
    *, job_id: str, arm: OD5aCompiledArm, items: tuple[OD5aWorkItem, ...],
) -> ValidatedOD5aPilot:
    """Reject any arm, workload or topology the durable pilot cannot run exactly."""
    if not job_id or arm.arm_id not in ALL_ARMS or not items:
        raise ValueError("registered arm, job ID and nonempty workload required")
    _require_compiled_arm(arm)
    stations = tuple(sorted(arm.model.stations))
    if not stations:
        raise ValueError("at least one physical station required")
    _require_items(items, stations)
    incoming, outgoing = _dependency_graph(arm, stations)
    raw_slots = arm.model.parameters["execution_slots"]
    if isinstance(raw_slots, bool) or not isinstance(raw_slots, int) or raw_slots < 1:
        raise ValueError("physical slots must be positive integers")
    coordination_latency, transition = _exact_intervals(arm, items)
    return ValidatedOD5aPilot(
        stations, incoming, outgoing, raw_slots, coordination_latency, transition,
    )


def _exact_intervals(
    arm: OD5aCompiledArm, items: tuple[OD5aWorkItem, ...],
) -> tuple[float, float]:
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
    return coordination_latency, transition


def _require_compiled_arm(arm: OD5aCompiledArm) -> None:
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


def _require_items(items: tuple[OD5aWorkItem, ...], stations: tuple[str, ...]) -> None:
    if len({x.item_id for x in items}) != len(items):
        raise ValueError("duplicate work items")
    if any({station for station, _ in x.service_by_station} != set(stations)
           or len(x.service_by_station) != len(stations) for x in items):
        raise ValueError("each item must provide each station's exogenous service demand")


def _dependency_graph(
    arm: OD5aCompiledArm, stations: tuple[str, ...],
) -> tuple[dict[str, list[str]], dict[str, list[str]]]:
    edges = tuple(tuple(edge) for edge in arm.model.routing["dependency_edges"])
    incoming: dict[str, list[str]] = {s: [] for s in stations}
    outgoing: dict[str, list[str]] = {s: [] for s in stations}
    for source, target in edges:
        if source not in outgoing or target not in incoming or source == target:
            raise ValueError("unknown dependency edge station")
        outgoing[source].append(target)
        incoming[target].append(source)
    return incoming, outgoing

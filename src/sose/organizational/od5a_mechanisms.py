"""OD-5a non-confirmatory SimPy mechanism pilot.

This adapter demonstrates *physical* execution slots and a separate,
contended coordination resource. It consumes fixed exogenous work durations;
it does not generate confirmatory worlds, persist resources, or satisfy the
OD-5a execution-freeze/restart gate. Only the five registered model arms may
be supplied through OD5aCompiledArm.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from decimal import Decimal
from datetime import datetime, timedelta, timezone
from math import isfinite

from sose.backends.simpy import SimPyBackend
from sose.core.identity import deterministic_id

from .od5a_admission import ALL_ARMS, OD5aCompiledArm


_ORIGIN = datetime(2000, 1, 1, tzinfo=timezone.utc)


def _require_microsecond_time(value: float | int, *, name: str) -> None:
    """Reject silently rounded intervals at the datetime-backed SimPy boundary."""
    ticks = Decimal(str(value)) * 1_000_000
    if ticks != ticks.to_integral_value():
        raise ValueError(f"{name} must be representable at microsecond resolution")


@dataclass(frozen=True, slots=True)
class OD5aWorkItem:
    """Precomputed exogenous service demands, unchanged between treatments."""

    item_id: str
    arrival_at: float
    service_by_station: tuple[tuple[str, float], ...]

    def __post_init__(self) -> None:
        if not self.item_id:
            raise ValueError("item ID cannot be empty")
        if isinstance(self.arrival_at, bool) or not isinstance(self.arrival_at, (float, int)):
            raise ValueError("arrival time must be numeric")
        if not isfinite(self.arrival_at) or self.arrival_at < 0:
            raise ValueError("arrival time must be finite and nonnegative")
        _require_microsecond_time(self.arrival_at, name="arrival time")
        if not self.service_by_station:
            raise ValueError("work item must have at least one station")
        names: set[str] = set()
        for station, duration in self.service_by_station:
            if not station or station in names:
                raise ValueError("station IDs must be nonempty and distinct")
            if isinstance(duration, bool) or not isinstance(duration, (int, float)):
                raise ValueError("service demand must be numeric")
            if not isfinite(duration) or duration <= 0:
                raise ValueError("service demand must be finite and positive")
            _require_microsecond_time(duration, name="service demand")
            names.add(station)


@dataclass(frozen=True, slots=True)
class OD5aMechanismEvent:
    kind: str
    item_id: str
    source: str
    target: str
    start: float
    end: float
    resource_instance: str
    cause: str
    model_spec_hash: str
    config_epoch: int = 0

    @property
    def duration(self) -> float:
        return self.end - self.start


@dataclass(frozen=True, slots=True)
class OD5aPilotResult:
    arm_id: str
    events: tuple[OD5aMechanismEvent, ...]
    completed_at: tuple[tuple[str, float], ...]
    incomplete_item_ids: tuple[str, ...]


def run_od5a_pilot(
    *, arm: OD5aCompiledArm, items: tuple[OD5aWorkItem, ...], horizon: float,
) -> OD5aPilotResult:
    """Run small deterministic falsification cases; NEVER confirmatory DOE."""
    if arm.arm_id not in ALL_ARMS:
        raise ValueError("pilot requires one of the five registered treatments")
    if isinstance(horizon, bool) or not isinstance(horizon, (int, float)):
        raise ValueError("horizon must be numeric")
    if not isfinite(horizon) or horizon <= 0:
        raise ValueError("pilot horizon must be finite and positive")
    _require_microsecond_time(horizon, name="pilot horizon")
    if not items or len({item.item_id for item in items}) != len(items):
        raise ValueError("pilot requires nonempty distinct work items")
    model = arm.model
    station_ids = tuple(sorted(model.stations))
    if not station_ids:
        raise ValueError("operational graph requires physical stations")
    if any({s for s, _ in item.service_by_station} != set(station_ids)
           or len(item.service_by_station) != len(station_ids) for item in items):
        raise ValueError("every item requires exactly one demand per configured station")

    raw_slots = model.parameters["execution_slots"]
    if isinstance(raw_slots, bool) or not isinstance(raw_slots, int) or raw_slots < 1:
        raise ValueError("physical execution_slots must be a positive integer")
    raw_latency = model.parameters["coordination_latency"]
    if isinstance(raw_latency, bool) or not isinstance(raw_latency, (float, int)):
        raise ValueError("coordination_latency must be numeric")
    if not isfinite(raw_latency) or raw_latency < 0:
        raise ValueError("coordination_latency must be finite and nonnegative")
    _require_microsecond_time(raw_latency, name="coordination latency")
    coordination_latency = float(raw_latency)

    incoming: dict[str, set[str]] = {station: set() for station in station_ids}
    outgoing: dict[str, list[str]] = {station: [] for station in station_ids}
    edges = model.routing.get("dependency_edges", ())
    for source, target in edges:
        if source not in incoming or target not in incoming or source == target:
            raise ValueError("dependency edge contains an unknown station")
        if source in incoming[target]:
            raise ValueError("duplicate dependency edge")
        incoming[target].add(source)
        outgoing[source].append(target)
    # The admission compiler verifies DAG acyclicity, so no edge can deadlock.

    transition_time = arm.charged_cost.transition_time if arm.charged_cost else 0.0
    _require_microsecond_time(transition_time, name="transition time")
    backend = SimPyBackend(origin=_ORIGIN)
    backend.create_resource("execution", capacity=raw_slots)
    backend.create_resource("coordination", capacity=1)

    available_slots = set(range(raw_slots))
    requested: set[tuple[str, str]] = set()
    finished: set[tuple[str, str]] = set()
    received: dict[tuple[str, str], set[str]] = defaultdict(set)
    events: list[OD5aMechanismEvent] = []
    completed_at: dict[str, float] = {}
    values = {
        item.item_id: dict(item.service_by_station)
        for item in items
    }
    model_hash = model.model_spec_hash

    def now() -> float:
        return (backend.now - _ORIGIN).total_seconds()

    def at(seconds: float) -> datetime:
        return _ORIGIN + timedelta(seconds=seconds)

    def start_stage(item_id: str, station: str) -> None:
        identity = (item_id, station)
        if identity in requested:
            raise RuntimeError("physical work stage requested more than once")
        requested.add(identity)

        def granted(lease) -> None:
            if not available_slots:
                raise RuntimeError("double physical executor allocation")
            slot = min(available_slots)
            available_slots.remove(slot)
            started = now()
            duration = values[item_id][station]

            def complete() -> None:
                ending = now()
                events.append(OD5aMechanismEvent(
                    kind="execution", item_id=item_id, source=station, target=station,
                    start=started, end=ending, resource_instance=f"execution:{slot}",
                    cause=deterministic_id("od5a-work-cause", item_id, station),
                    model_spec_hash=model_hash,
                ))
                available_slots.add(slot)
                backend.release_resource(lease)
                finished.add(identity)
                if all((item_id, node) in finished for node in station_ids):
                    completed_at[item_id] = ending
                for target in sorted(outgoing[station]):
                    start_handoff(item_id, station, target)

            backend.schedule_at(
                at(started + duration), complete,
                key=("od5a-work", item_id, station),
            )

        backend.request_resource(
            "execution",
            request_id=deterministic_id("od5a-execution-request", item_id, station),
            on_acquired=granted,
        )

    def start_handoff(item_id: str, source: str, target: str) -> None:
        def granted(lease) -> None:
            started = now()

            def complete() -> None:
                ending = now()
                events.append(OD5aMechanismEvent(
                    kind="coordination", item_id=item_id, source=source, target=target,
                    start=started, end=ending, resource_instance="coordination:0",
                    cause=deterministic_id("od5a-handoff-cause", item_id, source, target),
                    model_spec_hash=model_hash,
                ))
                backend.release_resource(lease)
                signals = received[(item_id, target)]
                if source in signals:
                    raise RuntimeError("dependency signal delivered more than once")
                signals.add(source)
                if signals == incoming[target]:
                    start_stage(item_id, target)

            backend.schedule_at(
                at(started + coordination_latency), complete,
                key=("od5a-handoff", item_id, source, target),
            )

        backend.request_resource(
            "coordination",
            request_id=deterministic_id("od5a-handoff-request", item_id, source, target),
            on_acquired=granted,
        )

    for item in items:
        start = max(float(item.arrival_at), transition_time)
        if start > horizon:
            continue

        def arrive(work_item: OD5aWorkItem = item) -> None:
            for root in station_ids:
                if not incoming[root]:
                    start_stage(work_item.item_id, root)

        backend.schedule_at(
            at(start), arrive, key=("od5a-arrival", item.item_id),
        )

    backend.run_until(at(horizon))
    assert len(available_slots) == raw_slots - backend.resource_snapshot("execution").in_use
    ordered_events = tuple(sorted(
        events, key=lambda e: (e.start, e.end, e.resource_instance, e.item_id, e.kind),
    ))
    intervals: dict[str, list[tuple[float, float]]] = defaultdict(list)
    for event in ordered_events:
        if event.end < event.start:
            raise RuntimeError("negative actor-time allocation")
        intervals[event.resource_instance].append((event.start, event.end))
    for occupancy in intervals.values():
        for left, right in zip(occupancy, occupancy[1:]):
            if left[1] > right[0]:
                raise RuntimeError("double allocation of a physical resource")
    return OD5aPilotResult(
        arm_id=arm.arm_id,
        events=ordered_events,
        completed_at=tuple(sorted(completed_at.items())),
        incomplete_item_ids=tuple(sorted(
            item.item_id for item in items if item.item_id not in completed_at
        )),
    )

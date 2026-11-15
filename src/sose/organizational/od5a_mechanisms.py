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
from functools import partial
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
            _validate_service_demand(duration)
            names.add(station)


def _validate_service_demand(duration) -> None:
    if isinstance(duration, bool) or not isinstance(duration, (int, float)):
        raise ValueError("service demand must be numeric")
    if not isfinite(duration) or duration <= 0:
        raise ValueError("service demand must be finite and positive")
    _require_microsecond_time(duration, name="service demand")


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
    _validate_pilot_horizon(horizon)
    if not items or len({item.item_id for item in items}) != len(items):
        raise ValueError("pilot requires nonempty distinct work items")
    model = arm.model
    station_ids = tuple(sorted(model.stations))
    _validate_station_demands(items, station_ids)
    raw_slots = _validated_execution_slots(model)
    coordination_latency = _validated_coordination_latency(model)
    incoming, outgoing = _dependency_graph(model, station_ids)
    # The admission compiler verifies DAG acyclicity, so no edge can deadlock.

    transition_time = arm.charged_cost.transition_time if arm.charged_cost else 0.0
    _require_microsecond_time(transition_time, name="transition time")
    run = _PilotRun(
        model=model, items=items, station_ids=station_ids, raw_slots=raw_slots,
        coordination_latency=coordination_latency, incoming=incoming, outgoing=outgoing,
    )
    run.schedule_arrivals(items, transition_time=transition_time, horizon=horizon)
    backend = run.backend
    backend.run_until(_at(horizon))
    assert len(run.available_slots) == raw_slots - backend.resource_snapshot("execution").in_use
    ordered_events = _ordered_checked_events(run.events)
    completed_at = run.completed_at
    return OD5aPilotResult(
        arm_id=arm.arm_id,
        events=ordered_events,
        completed_at=tuple(sorted(completed_at.items())),
        incomplete_item_ids=tuple(sorted(
            item.item_id for item in items if item.item_id not in completed_at
        )),
    )


def _validate_pilot_horizon(horizon) -> None:
    if isinstance(horizon, bool) or not isinstance(horizon, (int, float)):
        raise ValueError("horizon must be numeric")
    if not isfinite(horizon) or horizon <= 0:
        raise ValueError("pilot horizon must be finite and positive")
    _require_microsecond_time(horizon, name="pilot horizon")


def _validate_station_demands(
    items: tuple[OD5aWorkItem, ...], station_ids: tuple[str, ...],
) -> None:
    if not station_ids:
        raise ValueError("operational graph requires physical stations")
    if any({s for s, _ in item.service_by_station} != set(station_ids)
           or len(item.service_by_station) != len(station_ids) for item in items):
        raise ValueError("every item requires exactly one demand per configured station")


def _validated_execution_slots(model) -> int:
    raw_slots = model.parameters["execution_slots"]
    if isinstance(raw_slots, bool) or not isinstance(raw_slots, int) or raw_slots < 1:
        raise ValueError("physical execution_slots must be a positive integer")
    return raw_slots


def _validated_coordination_latency(model) -> float:
    raw_latency = model.parameters["coordination_latency"]
    if isinstance(raw_latency, bool) or not isinstance(raw_latency, (float, int)):
        raise ValueError("coordination_latency must be numeric")
    if not isfinite(raw_latency) or raw_latency < 0:
        raise ValueError("coordination_latency must be finite and nonnegative")
    _require_microsecond_time(raw_latency, name="coordination latency")
    return float(raw_latency)


def _dependency_graph(
    model, station_ids: tuple[str, ...],
) -> tuple[dict[str, set[str]], dict[str, list[str]]]:
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
    return incoming, outgoing


def _at(seconds: float) -> datetime:
    return _ORIGIN + timedelta(seconds=seconds)


class _PilotRun:
    """Mutable SimPy execution state of one pilot run (formerly its closures)."""

    def __init__(
        self, *, model, items: tuple[OD5aWorkItem, ...], station_ids: tuple[str, ...],
        raw_slots: int, coordination_latency: float,
        incoming: dict[str, set[str]], outgoing: dict[str, list[str]],
    ) -> None:
        self.station_ids = station_ids
        self.coordination_latency = coordination_latency
        self.incoming = incoming
        self.outgoing = outgoing
        self.backend = SimPyBackend(origin=_ORIGIN)
        self.backend.create_resource("execution", capacity=raw_slots)
        self.backend.create_resource("coordination", capacity=1)

        self.available_slots = set(range(raw_slots))
        self.requested: set[tuple[str, str]] = set()
        self.finished: set[tuple[str, str]] = set()
        self.received: dict[tuple[str, str], set[str]] = defaultdict(set)
        self.events: list[OD5aMechanismEvent] = []
        self.completed_at: dict[str, float] = {}
        self.values = {
            item.item_id: dict(item.service_by_station)
            for item in items
        }
        self.model_hash = model.model_spec_hash

    def now(self) -> float:
        return (self.backend.now - _ORIGIN).total_seconds()

    def start_stage(self, item_id: str, station: str) -> None:
        identity = (item_id, station)
        if identity in self.requested:
            raise RuntimeError("physical work stage requested more than once")
        self.requested.add(identity)
        self.backend.request_resource(
            "execution",
            request_id=deterministic_id("od5a-execution-request", item_id, station),
            on_acquired=partial(self._stage_granted, item_id, station),
        )

    def _stage_granted(self, item_id: str, station: str, lease) -> None:
        if not self.available_slots:
            raise RuntimeError("double physical executor allocation")
        slot = min(self.available_slots)
        self.available_slots.remove(slot)
        started = self.now()
        duration = self.values[item_id][station]
        self.backend.schedule_at(
            _at(started + duration),
            partial(self._stage_complete, item_id, station, lease, slot, started),
            key=("od5a-work", item_id, station),
        )

    def _stage_complete(
        self, item_id: str, station: str, lease, slot: int, started: float,
    ) -> None:
        identity = (item_id, station)
        ending = self.now()
        self.events.append(OD5aMechanismEvent(
            kind="execution", item_id=item_id, source=station, target=station,
            start=started, end=ending, resource_instance=f"execution:{slot}",
            cause=deterministic_id("od5a-work-cause", item_id, station),
            model_spec_hash=self.model_hash,
        ))
        self.available_slots.add(slot)
        self.backend.release_resource(lease)
        self.finished.add(identity)
        if all((item_id, node) in self.finished for node in self.station_ids):
            self.completed_at[item_id] = ending
        for target in sorted(self.outgoing[station]):
            self.start_handoff(item_id, station, target)

    def start_handoff(self, item_id: str, source: str, target: str) -> None:
        self.backend.request_resource(
            "coordination",
            request_id=deterministic_id("od5a-handoff-request", item_id, source, target),
            on_acquired=partial(self._handoff_granted, item_id, source, target),
        )

    def _handoff_granted(self, item_id: str, source: str, target: str, lease) -> None:
        started = self.now()
        self.backend.schedule_at(
            _at(started + self.coordination_latency),
            partial(self._handoff_complete, item_id, source, target, lease, started),
            key=("od5a-handoff", item_id, source, target),
        )

    def _handoff_complete(
        self, item_id: str, source: str, target: str, lease, started: float,
    ) -> None:
        ending = self.now()
        self.events.append(OD5aMechanismEvent(
            kind="coordination", item_id=item_id, source=source, target=target,
            start=started, end=ending, resource_instance="coordination:0",
            cause=deterministic_id("od5a-handoff-cause", item_id, source, target),
            model_spec_hash=self.model_hash,
        ))
        self.backend.release_resource(lease)
        signals = self.received[(item_id, target)]
        if source in signals:
            raise RuntimeError("dependency signal delivered more than once")
        signals.add(source)
        if signals == self.incoming[target]:
            self.start_stage(item_id, target)

    def schedule_arrivals(
        self, items: tuple[OD5aWorkItem, ...], *, transition_time: float, horizon: float,
    ) -> None:
        for item in items:
            start = max(float(item.arrival_at), transition_time)
            if start > horizon:
                continue
            self.backend.schedule_at(
                _at(start), partial(self._arrive, item), key=("od5a-arrival", item.item_id),
            )

    def _arrive(self, work_item: OD5aWorkItem) -> None:
        for root in self.station_ids:
            if not self.incoming[root]:
                self.start_stage(work_item.item_id, root)


def _ordered_checked_events(
    events: list[OD5aMechanismEvent],
) -> tuple[OD5aMechanismEvent, ...]:
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
    return ordered_events

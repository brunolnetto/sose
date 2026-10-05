from __future__ import annotations

from collections import deque
from datetime import datetime
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from .dataset import ObservedPRDataset
from .observations import ObservedEventKind, ObservedPRTrace
from .validation import DistributionSummary, summarize_distribution


NonBlankString = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]


class ObservedItemFlowCalibration(BaseModel):
    """Observable item-flow evidence; intentionally excludes inferred actor service capacity."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    observed_dataset_hash: NonBlankString
    item_count: int = Field(ge=1)
    observation_start: datetime
    observation_end: datetime
    interarrival_seconds: DistributionSummary | None = None
    arrival_rate_per_second: float | None = Field(default=None, gt=0.0, allow_inf_nan=False)
    lead_time_seconds: DistributionSummary
    review_response_latency_seconds: DistributionSummary | None = None
    ci_duration_seconds: DistributionSummary | None = None
    ci_gate_duration_seconds: DistributionSummary | None = None
    peak_wip: int = Field(ge=0)
    mean_wip: float = Field(ge=0.0, allow_inf_nan=False)
    unidentified_actor_parameters: tuple[NonBlankString, ...] = (
        "reviewer_service_time",
        "reviewer_capacity",
        "reviewer_calendar",
    )


def calibrate_observed_item_flow(dataset: ObservedPRDataset) -> ObservedItemFlowCalibration:
    """Derive only timestamp-identifiable item-flow evidence from terminal PR traces."""

    if any(trace.terminal_at is None for trace in dataset.traces):
        raise ValueError("item-flow calibration requires a terminal-only observed dataset")

    opened = tuple(trace.opened_at for trace in dataset.traces)
    terminals = tuple(_terminal(trace) for trace in dataset.traces)
    observation_start = min(opened)
    observation_end = max(terminals)

    lead_times = tuple(_lead_time(trace) for trace in dataset.traces)
    interarrivals = tuple(
        (later - earlier).total_seconds()
        for earlier, later in zip(opened, opened[1:])
    )

    arrival_span = (max(opened) - min(opened)).total_seconds()
    arrival_rate = None
    if len(opened) > 1 and arrival_span > 0.0:
        arrival_rate = (len(opened) - 1) / arrival_span

    review_latencies = tuple(
        latency
        for trace in dataset.traces
        for latency in trace.review_response_latencies_seconds()
    )
    ci_intervals_by_trace = tuple(_ci_intervals(trace) for trace in dataset.traces)
    ci_durations = tuple(
        (completed - started).total_seconds()
        for intervals in ci_intervals_by_trace
        for started, completed in intervals
    )
    ci_gate_durations = tuple(
        (max(completed for _, completed in intervals) - min(started for started, _ in intervals)).total_seconds()
        for intervals in ci_intervals_by_trace
        if intervals
    )

    peak_wip = _peak_wip(dataset.traces)
    observation_seconds = (observation_end - observation_start).total_seconds()
    total_item_seconds = sum(lead_times)
    mean_wip = total_item_seconds / observation_seconds if observation_seconds > 0.0 else 0.0

    return ObservedItemFlowCalibration(
        observed_dataset_hash=dataset.dataset_hash,
        item_count=len(dataset.traces),
        observation_start=observation_start,
        observation_end=observation_end,
        interarrival_seconds=(
            summarize_distribution(interarrivals, unit="seconds")
            if interarrivals
            else None
        ),
        arrival_rate_per_second=arrival_rate,
        lead_time_seconds=summarize_distribution(lead_times, unit="seconds"),
        review_response_latency_seconds=(
            summarize_distribution(review_latencies, unit="seconds")
            if review_latencies
            else None
        ),
        ci_duration_seconds=(
            summarize_distribution(ci_durations, unit="seconds")
            if ci_durations
            else None
        ),
        ci_gate_duration_seconds=(
            summarize_distribution(ci_gate_durations, unit="seconds")
            if ci_gate_durations
            else None
        ),
        peak_wip=peak_wip,
        mean_wip=mean_wip,
    )


def _terminal(trace: ObservedPRTrace) -> datetime:
    terminal = trace.terminal_at
    if terminal is None:  # guarded at the public boundary; keeps helper total.
        raise ValueError("item-flow calibration requires a terminal-only observed dataset")
    return terminal


def _lead_time(trace: ObservedPRTrace) -> float:
    lead_time = trace.lead_time_seconds
    if lead_time is None:  # guarded at the public boundary; keeps helper total.
        raise ValueError("item-flow calibration requires a terminal-only observed dataset")
    return lead_time


def _ci_intervals(trace: ObservedPRTrace) -> tuple[tuple[datetime, datetime], ...]:
    pending: deque[datetime] = deque()
    intervals: list[tuple[datetime, datetime]] = []
    for event in trace.events:
        if event.kind is ObservedEventKind.CI_STARTED:
            pending.append(event.occurred_at)
            continue
        if event.kind is not ObservedEventKind.CI_COMPLETED:
            continue
        if not pending:
            raise ValueError("observed trace contains CI_COMPLETED without matching CI_STARTED")
        intervals.append((pending.popleft(), event.occurred_at))
    return tuple(intervals)


def _peak_wip(traces: tuple[ObservedPRTrace, ...]) -> int:
    boundaries: list[tuple[datetime, int]] = []
    for trace in traces:
        terminal = _terminal(trace)
        if terminal <= trace.opened_at:
            continue
        boundaries.append((trace.opened_at, 1))
        boundaries.append((terminal, -1))

    # Half-open [opened, terminal): releases at t happen before arrivals at t.
    active = 0
    peak = 0
    for _, delta in sorted(boundaries, key=lambda item: (item[0], item[1])):
        active += delta
        peak = max(peak, active)
    return peak

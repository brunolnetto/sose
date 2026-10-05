from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from sose.organizational.calibration import calibrate_observed_item_flow
from sose.organizational.dataset import ObservedPRDataset
from sose.organizational.observations import ObservedEventKind, ObservedPREvent, ObservedPRTrace


BASE = datetime(2026, 1, 1, tzinfo=timezone.utc)


def _event(
    source_event_id: str,
    kind: ObservedEventKind,
    hours: float,
    *,
    actor_key: str | None = None,
) -> ObservedPREvent:
    return ObservedPREvent(
        source_event_id=source_event_id,
        kind=kind,
        occurred_at=BASE + timedelta(hours=hours),
        actor_key=actor_key,
    )


def _trace(
    number: int,
    *,
    opened_hour: float,
    terminal_hour: float | None,
    extras: tuple[ObservedPREvent, ...] = (),
) -> ObservedPRTrace:
    events = [_event(f"{number}:open", ObservedEventKind.OPENED, opened_hour)]
    events.extend(extras)
    if terminal_hour is not None:
        events.append(_event(f"{number}:merged", ObservedEventKind.MERGED, terminal_hour))
    return ObservedPRTrace(repository="example/repo", pr_number=number, events=tuple(events))


def _dataset(*traces: ObservedPRTrace) -> ObservedPRDataset:
    return ObservedPRDataset(dataset_version="1", traces=traces)


def test_calibration_binds_profile_to_terminal_training_dataset_hash() -> None:
    dataset = _dataset(
        _trace(1, opened_hour=0, terminal_hour=4),
        _trace(2, opened_hour=2, terminal_hour=8),
        _trace(3, opened_hour=5, terminal_hour=9),
    )

    profile = calibrate_observed_item_flow(dataset)

    assert profile.observed_dataset_hash == dataset.dataset_hash
    assert profile.item_count == 3
    assert profile.observation_start == BASE
    assert profile.observation_end == BASE + timedelta(hours=9)
    assert profile.lead_time_seconds.mean == pytest.approx((4 + 6 + 4) * 3600 / 3)
    assert profile.interarrival_seconds is not None
    assert profile.interarrival_seconds.mean == pytest.approx(2.5 * 3600)
    assert profile.arrival_rate_per_second == pytest.approx(2 / (5 * 3600))


def test_calibration_derives_peak_and_time_weighted_mean_wip() -> None:
    dataset = _dataset(
        _trace(1, opened_hour=0, terminal_hour=4),
        _trace(2, opened_hour=2, terminal_hour=8),
        _trace(3, opened_hour=5, terminal_hour=9),
    )

    profile = calibrate_observed_item_flow(dataset)

    assert profile.peak_wip == 2
    assert profile.mean_wip == pytest.approx(14 / 9)


def test_calibration_uses_elapsed_review_response_and_ci_machine_duration_only() -> None:
    dataset = _dataset(
        _trace(
            1,
            opened_hour=0,
            terminal_hour=8,
            extras=(
                _event("1:req", ObservedEventKind.REVIEW_REQUESTED, 1, actor_key="reviewer"),
                _event("1:review", ObservedEventKind.REVIEW_SUBMITTED, 3, actor_key="reviewer"),
                _event("1:ci-start", ObservedEventKind.CI_STARTED, 4),
                _event("1:ci-end", ObservedEventKind.CI_COMPLETED, 4.5),
            ),
        ),
        _trace(2, opened_hour=10, terminal_hour=12),
    )

    profile = calibrate_observed_item_flow(dataset)

    assert profile.review_response_latency_seconds is not None
    assert profile.review_response_latency_seconds.mean == pytest.approx(2 * 3600)
    assert profile.ci_duration_seconds is not None
    assert profile.ci_duration_seconds.mean == pytest.approx(0.5 * 3600)
    assert profile.ci_gate_duration_seconds is not None
    assert profile.ci_gate_duration_seconds.mean == pytest.approx(0.5 * 3600)
    assert "reviewer_service_time" in profile.unidentified_actor_parameters
    assert "reviewer_capacity" in profile.unidentified_actor_parameters
    assert "reviewer_calendar" in profile.unidentified_actor_parameters
    assert not hasattr(profile, "reviewer_service_time")


def test_calibration_pairs_multiple_ci_intervals_fifo_without_inventing_effort() -> None:
    dataset = _dataset(
        _trace(
            1,
            opened_hour=0,
            terminal_hour=8,
            extras=(
                _event("1:ci-start-a", ObservedEventKind.CI_STARTED, 1),
                _event("1:ci-end-a", ObservedEventKind.CI_COMPLETED, 2),
                _event("1:ci-start-b", ObservedEventKind.CI_STARTED, 3),
                _event("1:ci-end-b", ObservedEventKind.CI_COMPLETED, 5),
            ),
        ),
        _trace(2, opened_hour=10, terminal_hour=12),
    )

    profile = calibrate_observed_item_flow(dataset)

    assert profile.ci_duration_seconds is not None
    assert profile.ci_duration_seconds.count == 2
    assert profile.ci_duration_seconds.mean == pytest.approx(1.5 * 3600)
    assert profile.ci_gate_duration_seconds is not None
    assert profile.ci_gate_duration_seconds.count == 1
    assert profile.ci_gate_duration_seconds.mean == pytest.approx(4 * 3600)


def test_parallel_ci_jobs_produce_one_gate_latency_per_pr() -> None:
    dataset = _dataset(
        _trace(
            1,
            opened_hour=0,
            terminal_hour=2,
            extras=(
                _event("1:ci-start-a", ObservedEventKind.CI_STARTED, 0.25),
                _event("1:ci-start-b", ObservedEventKind.CI_STARTED, 0.25),
                _event("1:ci-end-a", ObservedEventKind.CI_COMPLETED, 0.25 + 1 / 60),
                _event("1:ci-end-b", ObservedEventKind.CI_COMPLETED, 0.75),
            ),
        )
    )

    profile = calibrate_observed_item_flow(dataset)

    assert profile.ci_duration_seconds is not None
    assert profile.ci_duration_seconds.count == 2
    assert profile.ci_duration_seconds.mean == pytest.approx(15.5 * 60)
    assert profile.ci_gate_duration_seconds is not None
    assert profile.ci_gate_duration_seconds.count == 1
    assert profile.ci_gate_duration_seconds.mean == pytest.approx(30 * 60)


def test_calibration_leaves_optional_summaries_absent_when_evidence_is_absent() -> None:
    dataset = _dataset(_trace(1, opened_hour=0, terminal_hour=2))

    profile = calibrate_observed_item_flow(dataset)

    assert profile.interarrival_seconds is None
    assert profile.arrival_rate_per_second is None
    assert profile.review_response_latency_seconds is None
    assert profile.ci_duration_seconds is None
    assert profile.ci_gate_duration_seconds is None
    assert profile.peak_wip == 1
    assert profile.mean_wip == pytest.approx(1.0)


def test_calibration_treats_zero_duration_traces_as_empty_half_open_intervals() -> None:
    dataset = _dataset(
        _trace(1, opened_hour=0, terminal_hour=0),
        _trace(2, opened_hour=0, terminal_hour=0),
    )

    profile = calibrate_observed_item_flow(dataset)

    assert profile.interarrival_seconds is not None
    assert profile.interarrival_seconds.mean == 0.0
    assert profile.arrival_rate_per_second is None
    assert profile.peak_wip == 0
    assert profile.mean_wip == 0.0


def test_calibration_rejects_nonterminal_data_to_protect_lead_time_and_wip_semantics() -> None:
    dataset = _dataset(
        _trace(1, opened_hour=0, terminal_hour=2),
        _trace(2, opened_hour=1, terminal_hour=None),
    )

    with pytest.raises(ValueError, match="terminal-only"):
        calibrate_observed_item_flow(dataset)


def test_calibration_rejects_ci_completion_without_a_matching_start() -> None:
    dataset = _dataset(
        _trace(
            1,
            opened_hour=0,
            terminal_hour=2,
            extras=(_event("1:ci-end", ObservedEventKind.CI_COMPLETED, 1),),
        )
    )

    with pytest.raises(ValueError, match="CI_COMPLETED without matching CI_STARTED"):
        calibrate_observed_item_flow(dataset)

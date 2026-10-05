from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from sose.organizational.dataset import ObservedPRDataset
from sose.organizational.observations import ObservedEventKind, ObservedPREvent, ObservedPRTrace
from sose.organizational.validation import (
    compare_lead_time_distributions,
    empirical_cdf_max_distance,
    summarize_distribution,
)


def _trace(number: int, opened_day: int, lead_hours: float) -> ObservedPRTrace:
    opened = datetime(2026, 1, opened_day, tzinfo=timezone.utc)
    terminal = opened + timedelta(hours=lead_hours)
    return ObservedPRTrace(
        repository="example/repo",
        pr_number=number,
        events=(
            ObservedPREvent(
                source_event_id=f"{number}:open",
                kind=ObservedEventKind.OPENED,
                occurred_at=opened,
            ),
            ObservedPREvent(
                source_event_id=f"{number}:merged",
                kind=ObservedEventKind.MERGED,
                occurred_at=terminal,
            ),
        ),
    )


def _dataset(hours: tuple[float, ...]) -> ObservedPRDataset:
    return ObservedPRDataset(
        dataset_version="1",
        traces=tuple(
            _trace(index, index, lead_hours)
            for index, lead_hours in enumerate(hours, start=1)
        ),
    )


def test_distribution_summary_has_unit_bearing_descriptive_statistics() -> None:
    summary = summarize_distribution((1.0, 2.0, 3.0, 4.0), unit="seconds")

    assert summary.count == 4
    assert summary.unit == "seconds"
    assert summary.mean == pytest.approx(2.5)
    assert summary.median == pytest.approx(2.5)
    assert summary.p90 == pytest.approx(4.0)
    assert not hasattr(summary, "score")


def test_summary_is_order_independent() -> None:
    assert summarize_distribution((3.0, 1.0, 2.0), unit="seconds") == summarize_distribution(
        (2.0, 3.0, 1.0), unit="seconds"
    )


def test_empirical_cdf_distance_is_zero_for_identical_distributions() -> None:
    assert empirical_cdf_max_distance((1.0, 2.0, 3.0), (3.0, 2.0, 1.0)) == 0.0


def test_empirical_cdf_distance_reaches_one_for_separated_samples() -> None:
    assert empirical_cdf_max_distance((1.0, 2.0, 3.0), (4.0, 5.0, 6.0)) == pytest.approx(1.0)


def test_lead_time_comparison_preserves_dataset_identity_and_seconds() -> None:
    observed = _dataset((1.0, 2.0, 3.0))
    simulated_seconds = (3600.0, 7200.0, 10800.0)

    result = compare_lead_time_distributions(
        observed=observed,
        simulated_seconds=simulated_seconds,
    )

    assert result.observed_dataset_hash == observed.dataset_hash
    assert result.observed.unit == "seconds"
    assert result.simulated.unit == "seconds"
    assert result.ecdf_max_distance == 0.0
    assert result.mean_difference_seconds == pytest.approx(0.0)
    assert result.median_difference_seconds == pytest.approx(0.0)
    assert not hasattr(result, "fit_score")


def test_lead_time_comparison_reports_directional_differences_without_collapsing_them() -> None:
    observed = _dataset((1.0, 2.0, 3.0))
    result = compare_lead_time_distributions(
        observed=observed,
        simulated_seconds=(7200.0, 10800.0, 14400.0),
    )

    assert result.mean_difference_seconds == pytest.approx(3600.0)
    assert result.median_difference_seconds == pytest.approx(3600.0)
    assert result.ecdf_max_distance > 0.0


def test_validation_rejects_open_observations() -> None:
    opened = datetime(2026, 1, 1, tzinfo=timezone.utc)
    open_trace = ObservedPRTrace(
        repository="example/repo",
        pr_number=1,
        events=(
            ObservedPREvent(
                source_event_id="open",
                kind=ObservedEventKind.OPENED,
                occurred_at=opened,
            ),
        ),
    )
    dataset = ObservedPRDataset(dataset_version="1", traces=(open_trace,))

    with pytest.raises(ValueError, match="terminal-only"):
        compare_lead_time_distributions(observed=dataset, simulated_seconds=(1.0,))


def test_distribution_metrics_reject_empty_nonfinite_and_negative_samples() -> None:
    for values in ((), (float("nan"),), (float("inf"),), (-1.0,)):
        with pytest.raises(ValueError):
            summarize_distribution(values, unit="seconds")

    with pytest.raises(ValueError):
        empirical_cdf_max_distance((), (1.0,))
    with pytest.raises(ValueError):
        empirical_cdf_max_distance((1.0,), ())

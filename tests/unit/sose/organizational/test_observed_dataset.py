from __future__ import annotations

from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from sose.organizational.dataset import ObservedPRDataset
from sose.organizational.observations import ObservedEventKind, ObservedPREvent, ObservedPRTrace


def _dt(day: int, hour: int = 0) -> datetime:
    return datetime(2026, 1, day, hour, tzinfo=timezone.utc)


def _trace(*, repository: str = "example/repo", number: int, opened_day: int, lead_hours: int = 4) -> ObservedPRTrace:
    opened = _dt(opened_day)
    closed = datetime.fromtimestamp(opened.timestamp() + lead_hours * 3600, tz=timezone.utc)
    return ObservedPRTrace(
        repository=repository,
        pr_number=number,
        events=(
            ObservedPREvent(
                source_event_id=f"{repository}:{number}:open",
                kind=ObservedEventKind.OPENED,
                occurred_at=opened,
            ),
            ObservedPREvent(
                source_event_id=f"{repository}:{number}:merged",
                kind=ObservedEventKind.MERGED,
                occurred_at=closed,
            ),
        ),
    )


def test_dataset_canonicalizes_trace_order_and_has_stable_hash() -> None:
    left = ObservedPRDataset(
        dataset_version="1",
        traces=(
            _trace(number=3, opened_day=3),
            _trace(number=1, opened_day=1),
            _trace(number=2, opened_day=2),
        ),
    )
    right = ObservedPRDataset(
        dataset_version="1",
        traces=(
            _trace(number=1, opened_day=1),
            _trace(number=2, opened_day=2),
            _trace(number=3, opened_day=3),
        ),
    )

    assert [trace.pr_number for trace in left.traces] == [1, 2, 3]
    assert left.canonical_json() == right.canonical_json()
    assert left.dataset_hash == right.dataset_hash
    assert len(left.dataset_hash) == 64


def test_dataset_rejects_duplicate_repository_pr_identity() -> None:
    duplicate = _trace(number=1, opened_day=1)
    with pytest.raises(ValidationError, match="duplicate observed pull request"):
        ObservedPRDataset(dataset_version="1", traces=(duplicate, duplicate))


def test_dataset_allows_multiple_repositories_but_keys_are_explicit() -> None:
    dataset = ObservedPRDataset(
        dataset_version="1",
        traces=(
            _trace(repository="a/repo", number=1, opened_day=1),
            _trace(repository="b/repo", number=1, opened_day=2),
        ),
    )
    assert dataset.keys == (("a/repo", 1), ("b/repo", 1))


def test_calibration_subset_requires_terminal_traces() -> None:
    open_trace = ObservedPRTrace(
        repository="example/repo",
        pr_number=99,
        events=(
            ObservedPREvent(
                source_event_id="open",
                kind=ObservedEventKind.OPENED,
                occurred_at=_dt(1),
            ),
        ),
    )
    dataset = ObservedPRDataset(
        dataset_version="1",
        traces=(open_trace, _trace(number=1, opened_day=2)),
    )

    terminal = dataset.terminal_only()
    assert terminal.keys == (("example/repo", 1),)
    assert terminal.dataset_hash != dataset.dataset_hash


def test_chronological_holdout_keeps_latest_items_and_has_no_overlap() -> None:
    dataset = ObservedPRDataset(
        dataset_version="1",
        traces=tuple(_trace(number=index, opened_day=index) for index in range(1, 11)),
    )

    split = dataset.chronological_holdout(holdout_fraction=0.3)

    assert [trace.pr_number for trace in split.train.traces] == list(range(1, 8))
    assert [trace.pr_number for trace in split.holdout.traces] == [8, 9, 10]
    assert set(split.train.keys).isdisjoint(split.holdout.keys)
    assert max(trace.opened_at for trace in split.train.traces) <= min(
        trace.opened_at for trace in split.holdout.traces
    )


def test_chronological_holdout_is_deterministic_for_equal_open_times() -> None:
    opened = _dt(1)
    traces = tuple(
        ObservedPRTrace(
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
                    occurred_at=datetime.fromtimestamp(opened.timestamp() + 3600, tz=timezone.utc),
                ),
            ),
        )
        for number in (4, 2, 3, 1)
    )
    dataset = ObservedPRDataset(dataset_version="1", traces=traces)

    split = dataset.chronological_holdout(holdout_fraction=0.5)
    assert split.train.keys == (("example/repo", 1), ("example/repo", 2))
    assert split.holdout.keys == (("example/repo", 3), ("example/repo", 4))


def test_holdout_fraction_requires_nonempty_train_and_holdout() -> None:
    dataset = ObservedPRDataset(
        dataset_version="1",
        traces=(_trace(number=1, opened_day=1), _trace(number=2, opened_day=2)),
    )
    with pytest.raises(ValueError, match="0 < holdout_fraction < 1"):
        dataset.chronological_holdout(holdout_fraction=0.0)
    with pytest.raises(ValueError, match="0 < holdout_fraction < 1"):
        dataset.chronological_holdout(holdout_fraction=1.0)


def test_holdout_requires_at_least_two_traces() -> None:
    dataset = ObservedPRDataset(dataset_version="1", traces=(_trace(number=1, opened_day=1),))
    with pytest.raises(ValueError, match="at least two"):
        dataset.chronological_holdout(holdout_fraction=0.5)

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from sose.organizational.dataset import ObservedPRDataset
from sose.organizational.observations import ObservedEventKind, ObservedPREvent, ObservedPRTrace


def _dt(day: int, hour: int = 0) -> datetime:
    return datetime(2026, 1, day, hour, tzinfo=timezone.utc)


def _trace(number: int, opened_day: int, terminal_day: int) -> ObservedPRTrace:
    return ObservedPRTrace(
        repository="example/repo",
        pr_number=number,
        events=(
            ObservedPREvent(
                source_event_id=f"{number}:open",
                kind=ObservedEventKind.OPENED,
                occurred_at=_dt(opened_day),
            ),
            ObservedPREvent(
                source_event_id=f"{number}:merged",
                kind=ObservedEventKind.MERGED,
                occurred_at=_dt(terminal_day),
            ),
        ),
    )


def test_holdout_purges_training_outcomes_that_cross_holdout_start() -> None:
    dataset = ObservedPRDataset(
        dataset_version="1",
        traces=(
            _trace(1, 1, 5),
            _trace(2, 2, 2),
            _trace(3, 3, 3),
            _trace(4, 4, 4),
        ),
    )

    split = dataset.chronological_holdout(holdout_fraction=0.5)

    assert split.train.keys == (("example/repo", 2),)
    assert split.holdout.keys == (("example/repo", 3), ("example/repo", 4))
    assert split.purged_keys == (("example/repo", 1),)
    cutoff = split.holdout.traces[0].opened_at
    assert all(trace.terminal_at is not None and trace.terminal_at <= cutoff for trace in split.train.traces)


def test_holdout_requires_terminal_outcomes_before_splitting() -> None:
    open_trace = ObservedPRTrace(
        repository="example/repo",
        pr_number=2,
        events=(
            ObservedPREvent(
                source_event_id="2:open",
                kind=ObservedEventKind.OPENED,
                occurred_at=_dt(2),
            ),
        ),
    )
    dataset = ObservedPRDataset(
        dataset_version="1",
        traces=(_trace(1, 1, 1), open_trace, _trace(3, 3, 3)),
    )

    with pytest.raises(ValueError, match="terminal-only"):
        dataset.chronological_holdout(holdout_fraction=1 / 3)


def test_holdout_rejects_split_when_purging_removes_all_training_data() -> None:
    dataset = ObservedPRDataset(
        dataset_version="1",
        traces=(
            _trace(1, 1, 4),
            _trace(2, 2, 4),
            _trace(3, 3, 3),
        ),
    )

    with pytest.raises(ValueError, match="purging leaves no training traces"):
        dataset.chronological_holdout(holdout_fraction=1 / 3)

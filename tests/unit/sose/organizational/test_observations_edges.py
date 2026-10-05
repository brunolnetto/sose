from __future__ import annotations

from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from sose.organizational.observations import ObservedEventKind, ObservedPREvent, ObservedPRTrace


def _ts(hour: int) -> datetime:
    return datetime(2026, 1, 1, hour, tzinfo=timezone.utc)


def test_metadata_must_be_finite_json_data() -> None:
    with pytest.raises(ValidationError, match="metadata"):
        ObservedPREvent(
            source_event_id="event",
            kind=ObservedEventKind.OPENED,
            occurred_at=_ts(0),
            metadata={"bad": object()},
        )
    with pytest.raises(ValidationError, match="metadata"):
        ObservedPREvent(
            source_event_id="event",
            kind=ObservedEventKind.OPENED,
            occurred_at=_ts(0),
            metadata={"bad": float("nan")},
        )


def test_trace_rejects_events_after_terminal_lifecycle_boundary() -> None:
    with pytest.raises(ValidationError, match="after terminal"):
        ObservedPRTrace(
            repository="example/repo",
            pr_number=1,
            events=(
                ObservedPREvent(
                    source_event_id="open",
                    kind=ObservedEventKind.OPENED,
                    occurred_at=_ts(0),
                ),
                ObservedPREvent(
                    source_event_id="merged",
                    kind=ObservedEventKind.MERGED,
                    occurred_at=_ts(2),
                ),
                ObservedPREvent(
                    source_event_id="late-review",
                    kind=ObservedEventKind.REVIEW_SUBMITTED,
                    occurred_at=_ts(3),
                    actor_key="reviewer",
                ),
            ),
        )


def test_repeated_review_requests_for_same_actor_pair_fifo() -> None:
    trace = ObservedPRTrace(
        repository="example/repo",
        pr_number=1,
        events=(
            ObservedPREvent(
                source_event_id="open",
                kind=ObservedEventKind.OPENED,
                occurred_at=_ts(0),
            ),
            ObservedPREvent(
                source_event_id="request-1",
                kind=ObservedEventKind.REVIEW_REQUESTED,
                occurred_at=_ts(1),
                actor_key="reviewer",
            ),
            ObservedPREvent(
                source_event_id="request-2",
                kind=ObservedEventKind.REVIEW_REQUESTED,
                occurred_at=_ts(2),
                actor_key="reviewer",
            ),
            ObservedPREvent(
                source_event_id="review-1",
                kind=ObservedEventKind.REVIEW_SUBMITTED,
                occurred_at=_ts(4),
                actor_key="reviewer",
            ),
            ObservedPREvent(
                source_event_id="review-2",
                kind=ObservedEventKind.REVIEW_SUBMITTED,
                occurred_at=_ts(6),
                actor_key="reviewer",
            ),
        ),
    )

    assert trace.review_response_latencies_seconds() == (3 * 3600, 4 * 3600)

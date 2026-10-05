from __future__ import annotations

from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from sose.organizational.observations import (
    ObservedEventKind,
    ObservedPREvent,
    ObservedPRTrace,
)


def _ts(hour: int) -> datetime:
    return datetime(2026, 1, 1, hour, tzinfo=timezone.utc)


def test_trace_preserves_raw_event_identity_and_orders_by_timestamp() -> None:
    trace = ObservedPRTrace(
        repository="example/repo",
        pr_number=42,
        events=(
            ObservedPREvent(
                source_event_id="review-1",
                kind=ObservedEventKind.REVIEW_SUBMITTED,
                occurred_at=_ts(3),
                actor_key="reviewer-a",
                state="approved",
            ),
            ObservedPREvent(
                source_event_id="pr-opened",
                kind=ObservedEventKind.OPENED,
                occurred_at=_ts(1),
                actor_key="author-a",
            ),
            ObservedPREvent(
                source_event_id="review-request-1",
                kind=ObservedEventKind.REVIEW_REQUESTED,
                occurred_at=_ts(2),
                actor_key="reviewer-a",
            ),
            ObservedPREvent(
                source_event_id="merged",
                kind=ObservedEventKind.MERGED,
                occurred_at=_ts(4),
                actor_key="maintainer-a",
            ),
        ),
    )

    assert [event.source_event_id for event in trace.events] == [
        "pr-opened",
        "review-request-1",
        "review-1",
        "merged",
    ]
    assert trace.opened_at == _ts(1)
    assert trace.terminal_at == _ts(4)
    assert trace.lead_time_seconds == 3 * 3600


def test_trace_rejects_duplicate_raw_event_identity() -> None:
    event = ObservedPREvent(
        source_event_id="same",
        kind=ObservedEventKind.OPENED,
        occurred_at=_ts(1),
    )
    with pytest.raises(ValidationError, match="duplicate source_event_id"):
        ObservedPRTrace(
            repository="example/repo",
            pr_number=1,
            events=(event, event),
        )


def test_trace_requires_exactly_one_open_and_at_most_one_terminal_event() -> None:
    with pytest.raises(ValidationError, match="exactly one opened"):
        ObservedPRTrace(
            repository="example/repo",
            pr_number=1,
            events=(
                ObservedPREvent(
                    source_event_id="review",
                    kind=ObservedEventKind.REVIEW_SUBMITTED,
                    occurred_at=_ts(2),
                ),
            ),
        )

    with pytest.raises(ValidationError, match="at most one terminal"):
        ObservedPRTrace(
            repository="example/repo",
            pr_number=1,
            events=(
                ObservedPREvent(
                    source_event_id="open",
                    kind=ObservedEventKind.OPENED,
                    occurred_at=_ts(1),
                ),
                ObservedPREvent(
                    source_event_id="closed",
                    kind=ObservedEventKind.CLOSED,
                    occurred_at=_ts(3),
                ),
                ObservedPREvent(
                    source_event_id="merged",
                    kind=ObservedEventKind.MERGED,
                    occurred_at=_ts(4),
                ),
            ),
        )


def test_trace_rejects_pre_open_events() -> None:
    with pytest.raises(ValidationError, match="before opened"):
        ObservedPRTrace(
            repository="example/repo",
            pr_number=1,
            events=(
                ObservedPREvent(
                    source_event_id="review",
                    kind=ObservedEventKind.REVIEW_SUBMITTED,
                    occurred_at=_ts(1),
                ),
                ObservedPREvent(
                    source_event_id="open",
                    kind=ObservedEventKind.OPENED,
                    occurred_at=_ts(2),
                ),
            ),
        )


def test_review_latency_is_observable_but_not_actor_service_time() -> None:
    trace = ObservedPRTrace(
        repository="example/repo",
        pr_number=7,
        events=(
            ObservedPREvent(
                source_event_id="open",
                kind=ObservedEventKind.OPENED,
                occurred_at=_ts(1),
            ),
            ObservedPREvent(
                source_event_id="request",
                kind=ObservedEventKind.REVIEW_REQUESTED,
                occurred_at=_ts(2),
                actor_key="reviewer-a",
            ),
            ObservedPREvent(
                source_event_id="submitted",
                kind=ObservedEventKind.REVIEW_SUBMITTED,
                occurred_at=_ts(5),
                actor_key="reviewer-a",
                state="changes_requested",
            ),
        ),
    )

    assert trace.review_response_latencies_seconds() == (3 * 3600,)
    assert not hasattr(trace, "reviewer_service_time")
    assert not hasattr(trace, "actor_utilization")


def test_review_response_pairs_by_actor_and_ignores_unmatched_requests() -> None:
    trace = ObservedPRTrace(
        repository="example/repo",
        pr_number=9,
        events=(
            ObservedPREvent(
                source_event_id="open",
                kind=ObservedEventKind.OPENED,
                occurred_at=_ts(0),
            ),
            ObservedPREvent(
                source_event_id="request-a",
                kind=ObservedEventKind.REVIEW_REQUESTED,
                occurred_at=_ts(1),
                actor_key="a",
            ),
            ObservedPREvent(
                source_event_id="request-b",
                kind=ObservedEventKind.REVIEW_REQUESTED,
                occurred_at=_ts(2),
                actor_key="b",
            ),
            ObservedPREvent(
                source_event_id="review-a",
                kind=ObservedEventKind.REVIEW_SUBMITTED,
                occurred_at=_ts(4),
                actor_key="a",
            ),
        ),
    )

    assert trace.review_response_latencies_seconds() == (3 * 3600,)


def test_event_timestamps_must_be_timezone_aware_and_ids_nonblank() -> None:
    with pytest.raises(ValidationError):
        ObservedPREvent(
            source_event_id="event",
            kind=ObservedEventKind.OPENED,
            occurred_at=datetime(2026, 1, 1, 0),
        )
    with pytest.raises(ValidationError):
        ObservedPREvent(
            source_event_id="",
            kind=ObservedEventKind.OPENED,
            occurred_at=_ts(1),
        )


def test_normalized_payload_is_deterministic() -> None:
    trace = ObservedPRTrace(
        repository="example/repo",
        pr_number=1,
        events=(
            ObservedPREvent(
                source_event_id="open",
                kind=ObservedEventKind.OPENED,
                occurred_at=_ts(1),
                metadata={"z": 2, "a": 1},
            ),
        ),
    )

    assert trace.canonical_json() == trace.canonical_json()
    assert '"a":1' in trace.canonical_json()

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from sose.organizational.empirical_pilot import (
    GitHubPRObservationSnapshot,
    GitHubPRSourceRecord,
    GitHubReviewSourceRecord,
    GitHubReviewTimelineSourceRecord,
)
from sose.organizational.observations import ObservedEventKind


T0 = datetime(2026, 10, 6, 15, 0, tzinfo=UTC)


def test_source_record_preserves_review_request_and_submission_evidence() -> None:
    record = GitHubPRSourceRecord(
        repository="brunolnetto/sose",
        pr_number=302,
        opened_at=T0,
        merged_at=T0 + timedelta(minutes=40),
        source_url="https://api.github.com/repos/brunolnetto/sose/pulls/302",
        review_timeline=(
            GitHubReviewTimelineSourceRecord(
                event_id=12,
                event="review_requested",
                occurred_at=T0 + timedelta(minutes=5),
                source_url="https://api.github.com/repos/brunolnetto/sose/issues/events/12",
                requested_actor_key="reviewer-a",
            ),
        ),
        submitted_reviews=(
            GitHubReviewSourceRecord(
                review_id=99,
                submitted_at=T0 + timedelta(minutes=20),
                state="APPROVED",
                actor_key="reviewer-a",
                source_url="https://api.github.com/repos/brunolnetto/sose/pulls/302/reviews/99",
            ),
        ),
    )

    trace = record.to_trace()
    assert [event.kind for event in trace.events] == [
        ObservedEventKind.OPENED,
        ObservedEventKind.REVIEW_REQUESTED,
        ObservedEventKind.REVIEW_SUBMITTED,
        ObservedEventKind.MERGED,
    ]
    assert trace.events[1].actor_key == "reviewer-a"
    assert trace.events[2].actor_key == "reviewer-a"
    assert trace.events[2].state == "approved"


def test_review_source_identity_is_canonical_and_order_independent() -> None:
    request = GitHubReviewTimelineSourceRecord(
        event_id=2,
        event="review_request_removed",
        occurred_at=T0 + timedelta(minutes=8),
        source_url="https://example.test/timeline/2",
        requested_actor_key="reviewer-b",
    )
    earlier = GitHubReviewTimelineSourceRecord(
        event_id=1,
        event="review_requested",
        occurred_at=T0 + timedelta(minutes=3),
        source_url="https://example.test/timeline/1",
        requested_actor_key="reviewer-b",
    )
    review = GitHubReviewSourceRecord(
        review_id=4,
        submitted_at=T0 + timedelta(minutes=6),
        state="CHANGES_REQUESTED",
        actor_key="reviewer-b",
        source_url="https://example.test/reviews/4",
    )

    kwargs = dict(
        repository="brunolnetto/sose",
        pr_number=302,
        opened_at=T0,
        merged_at=T0 + timedelta(minutes=10),
        source_url="https://example.test/pulls/302",
        submitted_reviews=(review,),
    )
    left = GitHubPRSourceRecord(review_timeline=(request, earlier), **kwargs)
    right = GitHubPRSourceRecord(review_timeline=(earlier, request), **kwargs)

    assert left == right
    assert left.canonical_payload() == right.canonical_payload()
    left_snapshot = GitHubPRObservationSnapshot(snapshot_version="v2", records=(left, _other_record()))
    right_snapshot = GitHubPRObservationSnapshot(snapshot_version="v2", records=(right, _other_record()))
    assert left_snapshot.snapshot_hash == right_snapshot.snapshot_hash


def test_duplicate_review_and_timeline_source_identities_are_rejected() -> None:
    review = GitHubReviewSourceRecord(
        review_id=1,
        submitted_at=T0 + timedelta(minutes=2),
        state="APPROVED",
        actor_key="reviewer-a",
        source_url="https://example.test/reviews/1",
    )
    timeline = GitHubReviewTimelineSourceRecord(
        event_id=7,
        event="review_requested",
        occurred_at=T0 + timedelta(minutes=1),
        source_url="https://example.test/timeline/7",
        requested_actor_key="reviewer-a",
    )
    base = dict(
        repository="brunolnetto/sose",
        pr_number=302,
        opened_at=T0,
        merged_at=T0 + timedelta(minutes=10),
        source_url="https://example.test/pulls/302",
    )

    with pytest.raises(ValueError, match="duplicate GitHub review identity"):
        GitHubPRSourceRecord(submitted_reviews=(review, review), **base)
    with pytest.raises(ValueError, match="duplicate GitHub review timeline identity"):
        GitHubPRSourceRecord(review_timeline=(timeline, timeline), **base)


def test_review_evidence_must_be_timezone_aware_and_supported() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        GitHubReviewSourceRecord(
            review_id=1,
            submitted_at=datetime(2026, 10, 6, 15, 0),
            state="approved",
            actor_key="reviewer",
            source_url="https://example.test/reviews/1",
        )

    with pytest.raises(ValueError, match="review_requested or review_request_removed"):
        GitHubReviewTimelineSourceRecord(
            event_id=1,
            event="commented",
            occurred_at=T0,
            source_url="https://example.test/timeline/1",
            requested_actor_key="reviewer",
        )


def _other_record() -> GitHubPRSourceRecord:
    return GitHubPRSourceRecord(
        repository="brunolnetto/sose",
        pr_number=303,
        opened_at=T0 + timedelta(hours=1),
        merged_at=T0 + timedelta(hours=2),
        source_url="https://example.test/pulls/303",
    )

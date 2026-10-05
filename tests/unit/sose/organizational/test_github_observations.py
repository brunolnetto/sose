from __future__ import annotations

import pytest

from sose.organizational.github_observations import normalize_github_pr_trace
from sose.organizational.observations import ObservedEventKind


def _pull_request(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "number": 42,
        "created_at": "2026-01-01T10:00:00Z",
        "closed_at": "2026-01-01T14:00:00Z",
        "merged_at": "2026-01-01T14:00:00Z",
        "user": {"login": "author"},
        "merged_by": {"login": "maintainer"},
    }
    payload.update(overrides)
    return payload


def test_adapter_normalizes_open_review_request_review_and_merge() -> None:
    trace = normalize_github_pr_trace(
        repository="example/repo",
        pull_request=_pull_request(),
        timeline_events=(
            {
                "id": 101,
                "event": "review_requested",
                "created_at": "2026-01-01T11:00:00Z",
                "requested_reviewer": {"login": "reviewer"},
            },
        ),
        reviews=(
            {
                "id": 201,
                "submitted_at": "2026-01-01T12:00:00Z",
                "state": "CHANGES_REQUESTED",
                "user": {"login": "reviewer"},
            },
        ),
    )

    assert trace.repository == "example/repo"
    assert trace.pr_number == 42
    assert [event.kind for event in trace.events] == [
        ObservedEventKind.OPENED,
        ObservedEventKind.REVIEW_REQUESTED,
        ObservedEventKind.REVIEW_SUBMITTED,
        ObservedEventKind.MERGED,
    ]
    assert trace.review_response_latencies_seconds() == (3600.0,)
    assert trace.lead_time_seconds == 4 * 3600
    assert trace.events[-1].actor_key == "maintainer"


def test_removed_review_request_is_cancelled_before_later_request_pairing() -> None:
    trace = normalize_github_pr_trace(
        repository="example/repo",
        pull_request=_pull_request(),
        timeline_events=(
            {
                "id": 101,
                "event": "review_requested",
                "created_at": "2026-01-01T10:30:00Z",
                "requested_reviewer": {"login": "reviewer"},
            },
            {
                "id": 102,
                "event": "review_request_removed",
                "created_at": "2026-01-01T11:00:00Z",
                "requested_reviewer": {"login": "reviewer"},
            },
            {
                "id": 103,
                "event": "review_requested",
                "created_at": "2026-01-01T12:00:00Z",
                "requested_reviewer": {"login": "reviewer"},
            },
        ),
        reviews=(
            {
                "id": 201,
                "submitted_at": "2026-01-01T13:00:00Z",
                "state": "APPROVED",
                "user": {"login": "reviewer"},
            },
        ),
    )

    assert [
        event.kind
        for event in trace.events
        if event.kind
        in {
            ObservedEventKind.REVIEW_REQUESTED,
            ObservedEventKind.REVIEW_REQUEST_REMOVED,
            ObservedEventKind.REVIEW_SUBMITTED,
        }
    ] == [
        ObservedEventKind.REVIEW_REQUESTED,
        ObservedEventKind.REVIEW_REQUEST_REMOVED,
        ObservedEventKind.REVIEW_REQUESTED,
        ObservedEventKind.REVIEW_SUBMITTED,
    ]
    assert trace.review_response_latencies_seconds() == (3600.0,)


def test_merged_pr_emits_one_terminal_event_not_closed_plus_merged() -> None:
    trace = normalize_github_pr_trace(
        repository="example/repo",
        pull_request=_pull_request(),
    )

    terminal = [
        event
        for event in trace.events
        if event.kind in {ObservedEventKind.CLOSED, ObservedEventKind.MERGED}
    ]
    assert len(terminal) == 1
    assert terminal[0].kind is ObservedEventKind.MERGED


def test_unmerged_closed_pr_emits_closed_terminal() -> None:
    trace = normalize_github_pr_trace(
        repository="example/repo",
        pull_request=_pull_request(merged_at=None, merged_by=None),
    )
    assert trace.events[-1].kind is ObservedEventKind.CLOSED


def test_equal_timestamp_request_precedes_review_across_endpoint_payloads() -> None:
    trace = normalize_github_pr_trace(
        repository="example/repo",
        pull_request=_pull_request(),
        timeline_events=(
            {
                "id": 101,
                "event": "review_requested",
                "created_at": "2026-01-01T11:00:00Z",
                "requested_reviewer": {"login": "reviewer"},
            },
        ),
        reviews=(
            {
                "id": 201,
                "submitted_at": "2026-01-01T11:00:00Z",
                "state": "APPROVED",
                "user": {"login": "reviewer"},
            },
        ),
    )

    assert trace.review_response_latencies_seconds() == (0.0,)


def test_adapter_normalizes_ci_jobs_without_inventing_human_actor_time() -> None:
    trace = normalize_github_pr_trace(
        repository="example/repo",
        pull_request=_pull_request(),
        workflow_jobs=(
            {
                "id": 301,
                "name": "tests",
                "started_at": "2026-01-01T10:05:00Z",
                "completed_at": "2026-01-01T10:15:00Z",
                "conclusion": "success",
            },
        ),
    )

    ci = [
        event
        for event in trace.events
        if event.kind in {ObservedEventKind.CI_STARTED, ObservedEventKind.CI_COMPLETED}
    ]
    assert [event.kind for event in ci] == [ObservedEventKind.CI_STARTED, ObservedEventKind.CI_COMPLETED]
    assert all(event.actor_key is None for event in ci)
    assert ci[-1].metadata["conclusion"] == "success"


def test_post_terminal_ci_activity_is_outside_pr_lifecycle_trace() -> None:
    trace = normalize_github_pr_trace(
        repository="example/repo",
        pull_request=_pull_request(),
        workflow_jobs=(
            {
                "id": 301,
                "name": "post-merge",
                "started_at": "2026-01-01T14:05:00Z",
                "completed_at": "2026-01-01T14:10:00Z",
                "conclusion": "success",
            },
        ),
    )

    assert all(event.kind not in {ObservedEventKind.CI_STARTED, ObservedEventKind.CI_COMPLETED} for event in trace.events)


def test_unsupported_timeline_events_and_unsubmitted_reviews_are_ignored() -> None:
    trace = normalize_github_pr_trace(
        repository="example/repo",
        pull_request=_pull_request(),
        timeline_events=(
            {"id": 1, "event": "mentioned", "created_at": "2026-01-01T11:00:00Z"},
        ),
        reviews=(
            {"id": 2, "submitted_at": None, "state": "PENDING", "user": {"login": "reviewer"}},
        ),
    )
    assert [event.kind for event in trace.events] == [ObservedEventKind.OPENED, ObservedEventKind.MERGED]


def test_adapter_rejects_missing_required_pull_request_identity_or_time() -> None:
    with pytest.raises(ValueError, match="number"):
        normalize_github_pr_trace(repository="example/repo", pull_request={"created_at": "2026-01-01T10:00:00Z"})
    with pytest.raises(ValueError, match="created_at"):
        normalize_github_pr_trace(repository="example/repo", pull_request={"number": 42})

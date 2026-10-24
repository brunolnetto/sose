"""#564 O1: source-observed review response only, never inferred reviewer effort.

Port the useful adversarial semantics of the historical empirical-review branch
to the currently supported GitHub normalization/ObservedPRTrace interfaces.
All timestamps and identities below are synthetic offline fixtures, not new
observations of any real reviewer.
"""
from __future__ import annotations

import pytest

from sose.organizational.github_observations import normalize_github_pr_trace
from sose.organizational.observations import ObservedEventKind


def _pr():
    return {
        "number": 258,
        "created_at": "2026-10-02T10:00:00Z",
        "merged_at": "2026-10-02T15:00:00Z",
        "user": {"login": "author"},
    }


def _request(event_id, reviewer, timestamp, *, event="review_requested"):
    return {
        "id": event_id,
        "event": event,
        "created_at": timestamp,
        "requested_reviewer": {"login": reviewer},
    }


def _review(review_id, reviewer, timestamp):
    return {
        "id": review_id,
        "state": "COMMENTED",
        "submitted_at": timestamp,
        "user": {"login": reviewer},
    }


def _trace(*, timeline_events=(), reviews=()):
    return normalize_github_pr_trace(
        repository="example/recorded-repo",
        pull_request=_pr(),
        timeline_events=timeline_events,
        reviews=reviews,
    )


def test_multiple_reviewers_pair_only_same_source_actor_and_keep_event_ids():
    trace = _trace(
        timeline_events=(
            _request(11, "alice", "2026-10-02T11:00:00Z"),
            _request(12, "bob", "2026-10-02T11:05:00Z"),
        ),
        reviews=(
            _review(21, "bob", "2026-10-02T11:15:00Z"),
            _review(22, "alice", "2026-10-02T11:20:00Z"),
        ),
    )
    assert trace.review_response_latencies_seconds() == (600.0, 1200.0)
    assert [e.source_event_id for e in trace.events if e.kind in {
        ObservedEventKind.REVIEW_REQUESTED, ObservedEventKind.REVIEW_SUBMITTED,
    }] == [
        "github:timeline:11:review_requested", "github:timeline:12:review_requested",
        "github:review:21", "github:review:22",
    ]
    assert not hasattr(trace, "reviewer_service_time")


def test_removed_request_and_unmatched_submission_never_estimate_effort():
    trace = _trace(
        timeline_events=(
            _request(31, "alice", "2026-10-02T11:00:00Z"),
            _request(32, "alice", "2026-10-02T11:05:00Z", event="review_request_removed"),
        ),
        reviews=(_review(41, "alice", "2026-10-02T12:00:00Z"),),
    )
    assert trace.review_response_latencies_seconds() == ()
    assert trace.lead_time_seconds == 5 * 3600


def test_team_request_cannot_be_silently_assigned_to_individual_reviewer():
    trace = _trace(
        timeline_events=({
            "id": 51, "event": "review_requested",
            "created_at": "2026-10-02T11:00:00Z",
            "requested_team": {"slug": "maintainers"},
        },),
        reviews=(_review(61, "alice", "2026-10-02T11:30:00Z"),),
    )
    request = next(e for e in trace.events if e.kind is ObservedEventKind.REVIEW_REQUESTED)
    assert request.actor_key == "team:maintainers"
    assert trace.review_response_latencies_seconds() == ()


def test_timezone_offsets_are_normalized_in_latency_without_clock_guessing():
    trace = _trace(
        timeline_events=(_request(71, "alice", "2026-10-02T08:00:00-03:00"),),
        reviews=(_review(81, "alice", "2026-10-02T11:10:00Z"),),
    )
    assert trace.review_response_latencies_seconds() == (600.0,)


def test_duplicate_review_source_id_fails_closed_not_double_counted():
    with pytest.raises(ValueError, match="duplicate source_event_id"):
        _trace(
            timeline_events=(_request(91, "alice", "2026-10-02T11:00:00Z"),),
            reviews=(
                _review(101, "alice", "2026-10-02T11:10:00Z"),
                _review(101, "alice", "2026-10-02T11:11:00Z"),
            ),
        )


def test_missing_reviewer_identity_does_not_invent_observed_response():
    trace = _trace(
        timeline_events=({
            "id": 111, "event": "review_requested",
            "created_at": "2026-10-02T11:00:00Z",
        },),
        reviews=(_review(121, "alice", "2026-10-02T11:20:00Z"),),
    )
    assert trace.review_response_latencies_seconds() == ()

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from sose.organizational.observations import ObservedEventKind
from sose.organizational.source_builder_v2 import build_github_pr_evidence_v2


T0 = datetime(2026, 10, 6, 15, 0, tzinfo=UTC)


def test_builder_preserves_author_reviews_and_workflow_run_provenance() -> None:
    record = build_github_pr_evidence_v2(
        repository="brunolnetto/sose",
        pull_request={
            "number": 304,
            "created_at": T0.isoformat(),
            "merged_at": (T0 + timedelta(minutes=30)).isoformat(),
            "url": "https://api.github.com/repos/brunolnetto/sose/pulls/304",
            "user": {"login": "renovate[bot]", "type": "Bot"},
        },
        timeline_events=(
            {
                "id": 8,
                "event": "commented",
                "created_at": (T0 + timedelta(minutes=2)).isoformat(),
                "url": "https://example.test/timeline/8",
            },
            {
                "id": 7,
                "event": "review_requested",
                "created_at": (T0 + timedelta(minutes=3)).isoformat(),
                "url": "https://example.test/timeline/7",
                "requested_team": {"slug": "core-maintainers"},
            },
        ),
        reviews=(
            {
                "id": 99,
                "submitted_at": (T0 + timedelta(minutes=12)).isoformat(),
                "state": "APPROVED",
                "url": "https://example.test/reviews/99",
                "user": {"login": "reviewer-a"},
            },
        ),
        workflow_jobs=(
            {
                "id": 9001,
                "workflow_id": 44,
                "run_id": 7001,
                "run_attempt": 2,
                "name": "coverage",
                "started_at": (T0 + timedelta(minutes=4)).isoformat(),
                "completed_at": (T0 + timedelta(minutes=8)).isoformat(),
                "conclusion": "success",
                "url": "https://example.test/actions/jobs/9001",
            },
        ),
    )

    assert record.author_actor_key == "renovate[bot]"
    assert record.author_is_bot is True
    assert len(record.review_timeline) == 1
    assert record.review_timeline[0].requested_actor_key == "team:core-maintainers"
    assert record.workflow_jobs[0].workflow_id == 44
    assert record.workflow_jobs[0].run_id == 7001
    assert record.workflow_jobs[0].run_attempt == 2

    trace = record.to_trace()
    assert trace.events[0].kind is ObservedEventKind.OPENED
    assert trace.events[0].actor_key == "renovate[bot]"
    assert trace.events[0].metadata["author_is_bot"] is True
    ci_started = next(event for event in trace.events if event.kind is ObservedEventKind.CI_STARTED)
    assert ci_started.metadata["workflow_id"] == 44
    assert ci_started.metadata["run_id"] == 7001
    assert ci_started.metadata["run_attempt"] == 2


def test_builder_is_order_independent_for_supported_evidence() -> None:
    timeline = (
        {
            "id": 2,
            "event": "review_request_removed",
            "created_at": (T0 + timedelta(minutes=8)).isoformat(),
            "url": "https://example.test/timeline/2",
            "requested_reviewer": {"login": "reviewer"},
        },
        {
            "id": 1,
            "event": "review_requested",
            "created_at": (T0 + timedelta(minutes=3)).isoformat(),
            "url": "https://example.test/timeline/1",
            "requested_reviewer": {"login": "reviewer"},
        },
    )
    kwargs = dict(
        repository="brunolnetto/sose",
        pull_request={
            "number": 304,
            "created_at": T0.isoformat(),
            "merged_at": (T0 + timedelta(minutes=30)).isoformat(),
            "url": "https://example.test/pulls/304",
            "user": {"login": "human", "type": "User"},
        },
    )

    left = build_github_pr_evidence_v2(timeline_events=timeline, **kwargs)
    right = build_github_pr_evidence_v2(timeline_events=tuple(reversed(timeline)), **kwargs)

    assert left == right
    assert left.canonical_payload() == right.canonical_payload()


def test_builder_requires_merged_pr_and_complete_workflow_identity() -> None:
    with pytest.raises(ValueError, match="merged_at"):
        build_github_pr_evidence_v2(
            repository="brunolnetto/sose",
            pull_request={
                "number": 304,
                "created_at": T0.isoformat(),
                "merged_at": None,
                "url": "https://example.test/pulls/304",
                "user": {"login": "human", "type": "User"},
            },
        )

    with pytest.raises(ValueError, match="workflow_id"):
        build_github_pr_evidence_v2(
            repository="brunolnetto/sose",
            pull_request={
                "number": 304,
                "created_at": T0.isoformat(),
                "merged_at": (T0 + timedelta(minutes=30)).isoformat(),
                "url": "https://example.test/pulls/304",
                "user": {"login": "human", "type": "User"},
            },
            workflow_jobs=(
                {
                    "id": 9001,
                    "run_id": 7001,
                    "run_attempt": 1,
                    "name": "coverage",
                    "started_at": (T0 + timedelta(minutes=4)).isoformat(),
                    "completed_at": (T0 + timedelta(minutes=8)).isoformat(),
                    "conclusion": "success",
                    "url": "https://example.test/actions/jobs/9001",
                },
            ),
        )


def test_builder_does_not_infer_bot_without_source_evidence_or_login_marker() -> None:
    record = build_github_pr_evidence_v2(
        repository="brunolnetto/sose",
        pull_request={
            "number": 304,
            "created_at": T0.isoformat(),
            "merged_at": (T0 + timedelta(minutes=30)).isoformat(),
            "url": "https://example.test/pulls/304",
            "user": {"login": "automation-service"},
        },
    )

    assert record.author_actor_key == "automation-service"
    assert record.author_is_bot is False

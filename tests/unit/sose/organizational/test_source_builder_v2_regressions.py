from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sose.organizational.source_builder_v2 import build_github_pr_evidence_v2


T0 = datetime(2026, 10, 6, 15, 0, tzinfo=UTC)


def _pr() -> dict[str, object]:
    return {
        "number": 304,
        "created_at": T0.isoformat(),
        "merged_at": (T0 + timedelta(minutes=30)).isoformat(),
        "url": "https://api.github.com/repos/brunolnetto/sose/pulls/304",
        "user": {"login": "human", "type": "User"},
    }


def test_native_workflow_job_uses_associated_run_for_workflow_identity() -> None:
    record = build_github_pr_evidence_v2(
        repository="brunolnetto/sose",
        pull_request=_pr(),
        workflow_runs=(
            {
                "id": 7001,
                "workflow_id": 44,
                "run_attempt": 2,
                "url": "https://api.github.com/repos/brunolnetto/sose/actions/runs/7001",
            },
        ),
        workflow_jobs=(
            {
                "id": 9001,
                "run_id": 7001,
                "run_attempt": 2,
                "name": "coverage",
                "started_at": (T0 + timedelta(minutes=4)).isoformat(),
                "completed_at": (T0 + timedelta(minutes=8)).isoformat(),
                "conclusion": "success",
                "url": "https://api.github.com/repos/brunolnetto/sose/actions/jobs/9001",
            },
        ),
    )

    job = record.workflow_jobs[0]
    assert job.workflow_id == 44
    assert job.run_id == 7001
    assert job.run_attempt == 2


def test_pending_reviews_are_not_promoted_to_submitted_review_evidence() -> None:
    record = build_github_pr_evidence_v2(
        repository="brunolnetto/sose",
        pull_request=_pr(),
        reviews=(
            {
                "id": 98,
                "submitted_at": None,
                "state": "PENDING",
                "url": "https://example.test/reviews/98",
                "user": {"login": "reviewer-a"},
            },
            {
                "id": 99,
                "submitted_at": (T0 + timedelta(minutes=12)).isoformat(),
                "state": "APPROVED",
                "url": "https://example.test/reviews/99",
                "user": {"login": "reviewer-a"},
            },
        ),
    )

    assert [review.review_id for review in record.submitted_reviews] == [99]

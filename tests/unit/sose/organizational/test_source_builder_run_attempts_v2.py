from __future__ import annotations

from sose.organizational.source_builder_v2 import build_github_pr_evidence_v2


def test_jobs_from_prior_attempts_are_valid_against_latest_run_provenance() -> None:
    record = build_github_pr_evidence_v2(
        repository="brunolnetto/sose",
        pull_request={
            "number": 302,
            "created_at": "2026-10-06T14:48:17Z",
            "merged_at": "2026-10-06T15:09:10Z",
            "url": "https://api.github.com/repos/brunolnetto/sose/pulls/302",
            "user": {"login": "brunolnetto", "type": "User"},
        },
        workflow_runs=({"id": 10, "workflow_id": 20, "run_attempt": 2},),
        workflow_jobs=(
            {
                "id": 30,
                "run_id": 10,
                "run_attempt": 1,
                "name": "pytest",
                "started_at": "2026-10-06T14:50:00Z",
                "completed_at": "2026-10-06T14:51:00Z",
                "conclusion": "failure",
                "url": "https://api.github.com/repos/brunolnetto/sose/actions/jobs/30",
            },
            {
                "id": 31,
                "run_id": 10,
                "run_attempt": 2,
                "name": "pytest",
                "started_at": "2026-10-06T14:52:00Z",
                "completed_at": "2026-10-06T14:53:00Z",
                "conclusion": "success",
                "url": "https://api.github.com/repos/brunolnetto/sose/actions/jobs/31",
            },
        ),
    )

    assert tuple(job.run_attempt for job in record.workflow_jobs) == (1, 2)
    assert tuple(job.conclusion for job in record.workflow_jobs) == ("failure", "success")

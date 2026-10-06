from __future__ import annotations

import pytest

from sose.organizational.source_builder_v2 import build_github_pr_evidence_v2


def test_job_attempt_cannot_exceed_latest_known_run_attempt() -> None:
    with pytest.raises(ValueError, match="run_attempt exceeds associated workflow run attempt"):
        build_github_pr_evidence_v2(
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
                    "id": 32,
                    "run_id": 10,
                    "run_attempt": 3,
                    "name": "pytest",
                    "started_at": "2026-10-06T14:54:00Z",
                    "completed_at": "2026-10-06T14:55:00Z",
                    "conclusion": "success",
                    "url": "https://api.github.com/repos/brunolnetto/sose/actions/jobs/32",
                },
            ),
        )

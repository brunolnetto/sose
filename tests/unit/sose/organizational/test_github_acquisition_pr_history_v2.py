from __future__ import annotations

from datetime import UTC, datetime

from sose.organizational.github_acquisition_v2 import GitHubJsonPage, acquire_github_pr_v2


REPOSITORY = "brunolnetto/sose"
API = f"https://api.github.com/repos/{REPOSITORY}"
PR_URL = f"{API}/pulls/302"
TIMELINE_URL = f"{API}/issues/302/timeline?per_page=100&page=1"
REVIEWS_URL = f"{API}/pulls/302/reviews?per_page=100&page=1"
RUNS_URL = (
    f"{API}/actions/runs?event=pull_request&created=2026-10-06..2026-10-06"
    "&per_page=100&page=1"
)
JOBS_10_URL = f"{API}/actions/runs/10/jobs?filter=all&per_page=100&page=1"
JOBS_11_URL = f"{API}/actions/runs/11/jobs?filter=all&per_page=100&page=1"


class FakeClient:
    def __init__(self, pages: dict[str, GitHubJsonPage]) -> None:
        self.pages = pages
        self.requests: list[str] = []

    def get_json(self, url: str) -> GitHubJsonPage:
        self.requests.append(url)
        try:
            return self.pages[url]
        except KeyError as exc:
            raise AssertionError(f"unexpected request {url}") from exc


def _run(run_id: int, pr_number: int, attempt: int = 1) -> dict[str, object]:
    return {
        "id": run_id,
        "workflow_id": 20,
        "run_attempt": attempt,
        "pull_requests": [{"number": pr_number}],
    }


def _job(job_id: int, run_id: int, attempt: int, conclusion: str) -> dict[str, object]:
    return {
        "id": job_id,
        "run_id": run_id,
        "run_attempt": attempt,
        "name": "pytest",
        "started_at": "2026-10-06T14:50:00Z",
        "completed_at": "2026-10-06T14:51:00Z",
        "conclusion": conclusion,
        "url": f"{API}/actions/jobs/{job_id}",
    }


def test_acquisition_filters_runs_by_pr_identity_and_keeps_all_attempts() -> None:
    client = FakeClient(
        {
            PR_URL: GitHubJsonPage(
                source_url=PR_URL,
                payload={
                    "number": 302,
                    "created_at": "2026-10-06T14:48:17Z",
                    "merged_at": "2026-10-06T15:09:10Z",
                    "url": PR_URL,
                    "base": {"repo": {"full_name": REPOSITORY}},
                    "head": {"sha": "final-head-only"},
                    "user": {"login": "brunolnetto", "type": "User"},
                },
            ),
            TIMELINE_URL: GitHubJsonPage(source_url=TIMELINE_URL, payload=[]),
            REVIEWS_URL: GitHubJsonPage(source_url=REVIEWS_URL, payload=[]),
            RUNS_URL: GitHubJsonPage(
                source_url=RUNS_URL,
                payload={
                    "total_count": 3,
                    "workflow_runs": [
                        _run(10, 302, attempt=1),
                        _run(11, 302, attempt=2),
                        _run(12, 999, attempt=1),
                    ],
                },
            ),
            JOBS_10_URL: GitHubJsonPage(
                source_url=JOBS_10_URL,
                payload={"total_count": 1, "jobs": [_job(30, 10, 1, "success")]},
            ),
            JOBS_11_URL: GitHubJsonPage(
                source_url=JOBS_11_URL,
                payload={
                    "total_count": 2,
                    "jobs": [
                        _job(31, 11, 1, "failure"),
                        _job(32, 11, 2, "success"),
                    ],
                },
            ),
        }
    )

    artifact = acquire_github_pr_v2(
        repository=REPOSITORY,
        pr_number=302,
        client=client,
        captured_at=datetime(2026, 10, 6, 18, 0, tzinfo=UTC),
    )

    assert artifact.manifest.workflow_runs.record_count == 2
    assert artifact.manifest.workflow_jobs.record_count == 3
    assert tuple((job.run_id, job.run_attempt) for job in artifact.evidence.workflow_jobs) == (
        (10, 1),
        (11, 1),
        (11, 2),
    )
    assert not any("runs/12/jobs" in request for request in client.requests)
    assert JOBS_10_URL in client.requests
    assert JOBS_11_URL in client.requests

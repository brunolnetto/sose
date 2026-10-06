from __future__ import annotations

from datetime import UTC, datetime

import pytest

from sose.organizational.acquisition_v2 import GitHubEndpointCaptureV2
from sose.organizational.github_acquisition_v2 import (
    GitHubJsonPage,
    acquire_github_pr_v2,
)


REPOSITORY = "brunolnetto/sose"
CAPTURED_AT = datetime(2026, 10, 6, 18, 0, tzinfo=UTC)


class FakeClient:
    def __init__(self, pages: dict[str, GitHubJsonPage]) -> None:
        self.pages = pages
        self.requests: list[str] = []

    def get_json(self, url: str) -> GitHubJsonPage:
        self.requests.append(url)
        try:
            return self.pages[url]
        except KeyError as exc:
            raise AssertionError(f"unexpected GitHub request: {url}") from exc


def _pr_url(number: int = 302) -> str:
    return f"https://api.github.com/repos/{REPOSITORY}/pulls/{number}"


def _timeline_url(number: int = 302, page: int = 1) -> str:
    return f"https://api.github.com/repos/{REPOSITORY}/issues/{number}/timeline?per_page=100&page={page}"


def _reviews_url(number: int = 302, page: int = 1) -> str:
    return f"https://api.github.com/repos/{REPOSITORY}/pulls/{number}/reviews?per_page=100&page={page}"


def _runs_url(page: int = 1) -> str:
    return (
        f"https://api.github.com/repos/{REPOSITORY}/actions/runs"
        f"?event=pull_request&created=2026-10-06..2026-10-06&per_page=100&page={page}"
    )


def _jobs_url(run_id: int, page: int = 1) -> str:
    return (
        f"https://api.github.com/repos/{REPOSITORY}/actions/runs/{run_id}/jobs"
        f"?filter=all&per_page=100&page={page}"
    )


def _pr(number: int = 302) -> dict[str, object]:
    return {
        "number": number,
        "created_at": "2026-10-06T14:48:17Z",
        "merged_at": "2026-10-06T15:09:10Z",
        "url": _pr_url(number),
        "base": {"repo": {"full_name": REPOSITORY}},
        "user": {"login": "brunolnetto", "type": "User"},
    }


def _review() -> dict[str, object]:
    return {
        "id": 5430265821,
        "submitted_at": "2026-10-06T14:53:22Z",
        "state": "COMMENTED",
        "url": f"{_pr_url()}/reviews/5430265821",
        "user": {"login": "chatgpt-codex-connector[bot]", "type": "Bot"},
    }


def _run(run_id: int = 10, *, pr_number: int = 302, attempt: int = 1) -> dict[str, object]:
    return {
        "id": run_id,
        "workflow_id": 20,
        "run_attempt": attempt,
        "pull_requests": [{"number": pr_number}],
    }


def _job(run_id: int = 10, job_id: int = 30, *, attempt: int = 1) -> dict[str, object]:
    return {
        "id": job_id,
        "run_id": run_id,
        "run_attempt": attempt,
        "name": "pytest (Python 3.14)",
        "started_at": "2026-10-06T14:50:00Z",
        "completed_at": "2026-10-06T14:51:00Z",
        "conclusion": "success",
        "url": f"https://api.github.com/repos/{REPOSITORY}/actions/jobs/{job_id}",
    }


def _base_pages() -> dict[str, GitHubJsonPage]:
    return {
        _pr_url(): GitHubJsonPage(source_url=_pr_url(), payload=_pr()),
        _timeline_url(): GitHubJsonPage(source_url=_timeline_url(), payload=[]),
        _reviews_url(): GitHubJsonPage(source_url=_reviews_url(), payload=[_review()]),
        _runs_url(): GitHubJsonPage(
            source_url=_runs_url(),
            payload={"total_count": 1, "workflow_runs": [_run()]},
        ),
        _jobs_url(10): GitHubJsonPage(
            source_url=_jobs_url(10),
            payload={"total_count": 1, "jobs": [_job()]},
        ),
    }


def test_acquire_one_pr_builds_complete_manifest_and_evidence() -> None:
    client = FakeClient(_base_pages())

    artifact = acquire_github_pr_v2(
        repository=REPOSITORY,
        pr_number=302,
        client=client,
        captured_at=CAPTURED_AT,
    )

    assert artifact.evidence.pr_number == 302
    assert len(artifact.evidence.submitted_reviews) == 1
    assert len(artifact.evidence.workflow_jobs) == 1
    assert artifact.manifest.timeline.record_count == 0
    assert artifact.manifest.reviews.record_count == 1
    assert artifact.manifest.workflow_runs.record_count == 1
    assert artifact.manifest.workflow_jobs.record_count == 1
    assert artifact.manifest.is_complete
    assert client.requests == [
        _pr_url(),
        _timeline_url(),
        _reviews_url(),
        _runs_url(),
        _jobs_url(10),
    ]


def test_acquisition_follows_next_page_urls_and_records_every_page() -> None:
    pages = _base_pages()
    pages[_reviews_url()] = GitHubJsonPage(
        source_url=_reviews_url(),
        payload=[_review()],
        next_url=_reviews_url(page=2),
    )
    pages[_reviews_url(page=2)] = GitHubJsonPage(
        source_url=_reviews_url(page=2),
        payload=[
            {
                **_review(),
                "id": 5430265822,
                "submitted_at": "2026-10-06T14:54:22Z",
                "url": f"{_pr_url()}/reviews/5430265822",
            }
        ],
    )

    artifact = acquire_github_pr_v2(
        repository=REPOSITORY,
        pr_number=302,
        client=FakeClient(pages),
        captured_at=CAPTURED_AT,
    )

    assert artifact.manifest.reviews.pages_fetched == 2
    assert artifact.manifest.reviews.record_count == 2
    assert artifact.manifest.reviews.source_urls == (_reviews_url(), _reviews_url(page=2))
    assert len(artifact.evidence.submitted_reviews) == 2


def test_acquisition_collects_job_pages_for_each_workflow_run() -> None:
    pages = _base_pages()
    pages[_runs_url()] = GitHubJsonPage(
        source_url=_runs_url(),
        payload={"total_count": 2, "workflow_runs": [_run(10), _run(11)]},
    )
    pages[_jobs_url(11)] = GitHubJsonPage(
        source_url=_jobs_url(11),
        payload={"total_count": 1, "jobs": [_job(run_id=11, job_id=31)]},
    )

    artifact = acquire_github_pr_v2(
        repository=REPOSITORY,
        pr_number=302,
        client=FakeClient(pages),
        captured_at=CAPTURED_AT,
    )

    assert artifact.manifest.workflow_jobs.pages_fetched == 2
    assert artifact.manifest.workflow_jobs.record_count == 2
    assert tuple(job.run_id for job in artifact.evidence.workflow_jobs) == (10, 11)


def test_unrelated_candidate_runs_are_not_admitted_or_queried_for_jobs() -> None:
    pages = _base_pages()
    pages[_runs_url()] = GitHubJsonPage(
        source_url=_runs_url(),
        payload={
            "total_count": 2,
            "workflow_runs": [_run(10), _run(99, pr_number=999)],
        },
    )
    client = FakeClient(pages)

    artifact = acquire_github_pr_v2(
        repository=REPOSITORY,
        pr_number=302,
        client=client,
        captured_at=CAPTURED_AT,
    )

    assert artifact.manifest.workflow_runs.record_count == 1
    assert not any("runs/99/jobs" in request for request in client.requests)


def test_zero_workflow_runs_prove_zero_jobs_without_inventing_a_jobs_request() -> None:
    pages = _base_pages()
    pages[_runs_url()] = GitHubJsonPage(
        source_url=_runs_url(), payload={"total_count": 0, "workflow_runs": []}
    )
    del pages[_jobs_url(10)]
    client = FakeClient(pages)

    artifact = acquire_github_pr_v2(
        repository=REPOSITORY,
        pr_number=302,
        client=client,
        captured_at=CAPTURED_AT,
    )

    assert artifact.manifest.workflow_runs.record_count == 0
    assert artifact.manifest.workflow_jobs.record_count == 0
    assert artifact.manifest.workflow_jobs.pages_fetched == 0
    assert artifact.manifest.workflow_jobs.source_urls == ()
    assert not any("/jobs" in request for request in client.requests)


def test_zero_page_capture_is_valid_only_for_jobs_derived_from_zero_runs() -> None:
    zero_jobs = GitHubEndpointCaptureV2(
        endpoint="workflow_jobs",
        pages_fetched=0,
        record_count=0,
        complete=True,
        source_urls=(),
    )
    assert zero_jobs.complete

    with pytest.raises(ValueError, match="zero-page capture"):
        GitHubEndpointCaptureV2(
            endpoint="reviews",
            pages_fetched=0,
            record_count=0,
            complete=True,
            source_urls=(),
        )


def test_pr_must_be_merged() -> None:
    pages = _base_pages()
    pages[_pr_url()] = GitHubJsonPage(
        source_url=_pr_url(),
        payload={**_pr(), "merged_at": None},
    )
    with pytest.raises(ValueError, match="merged pull request"):
        acquire_github_pr_v2(
            repository=REPOSITORY,
            pr_number=302,
            client=FakeClient(pages),
            captured_at=CAPTURED_AT,
        )


def test_malformed_collection_payloads_are_rejected() -> None:
    pages = _base_pages()
    pages[_runs_url()] = GitHubJsonPage(source_url=_runs_url(), payload={"workflow_runs": "bad"})

    with pytest.raises(ValueError, match="workflow_runs list"):
        acquire_github_pr_v2(
            repository=REPOSITORY,
            pr_number=302,
            client=FakeClient(pages),
            captured_at=CAPTURED_AT,
        )

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol
from urllib.error import HTTPError
from urllib.request import Request, urlopen
import json

from .acquisition_v2 import (
    GitHubEndpointCaptureV2,
    GitHubPRAcquisitionArtifactV2,
    GitHubPRAcquisitionManifestV2,
    build_complete_github_pr_evidence_v2,
)


@dataclass(frozen=True, slots=True)
class GitHubJsonPage:
    """One materialized GitHub REST page and its pagination successor."""

    source_url: str
    payload: object
    next_url: str | None = None


class GitHubJsonClient(Protocol):
    def get_json(self, url: str) -> GitHubJsonPage: ...


class UrllibGitHubJsonClient:
    """Small GitHub REST client whose page boundaries remain explicit evidence."""

    def __init__(self, *, token: str | None = None, user_agent: str = "sose-organizational-v2") -> None:
        self._token = token
        self._user_agent = user_agent

    def get_json(self, url: str) -> GitHubJsonPage:
        headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": self._user_agent,
        }
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"
        request = Request(url, headers=headers)
        try:
            with urlopen(request) as response:  # noqa: S310 - GitHub URL supplied by this module
                payload = json.loads(response.read().decode("utf-8"))
                next_url = _next_link(response.headers.get("Link"))
                final_url = response.geturl()
        except HTTPError as exc:
            raise RuntimeError(f"GitHub acquisition failed with HTTP {exc.code}: {url}") from exc
        return GitHubJsonPage(source_url=final_url, payload=payload, next_url=next_url)


def acquire_github_pr_v2(
    *,
    repository: str,
    pr_number: int,
    client: GitHubJsonClient,
    captured_at: datetime,
) -> GitHubPRAcquisitionArtifactV2:
    """Acquire every GitHub source required for one prospective v2 PR record.

    Workflow runs are discovered over the PR's open-to-merge date window and are
    then correlated by the native ``pull_requests`` identity. This preserves CI
    from every synchronized revision instead of observing only the final head SHA.
    Jobs use ``filter=all`` so rerun attempts remain evidence rather than being
    overwritten by the latest attempt.
    """

    repository = repository.strip()
    if not repository or "/" not in repository:
        raise ValueError("repository must be owner/name")
    if pr_number < 1:
        raise ValueError("pr_number must be positive")

    api = f"https://api.github.com/repos/{repository}"
    pr_url = f"{api}/pulls/{pr_number}"
    pr_page = client.get_json(pr_url)
    pull_request = _require_mapping(pr_page.payload, name="pull request")
    merged_at = pull_request.get("merged_at")
    if merged_at is None:
        raise ValueError("prospective acquisition requires a merged pull request")
    opened_date = _iso_date(pull_request.get("created_at"), name="created_at")
    merged_date = _iso_date(merged_at, name="merged_at")

    timeline_url = f"{api}/issues/{pr_number}/timeline?per_page=100&page=1"
    reviews_url = f"{api}/pulls/{pr_number}/reviews?per_page=100&page=1"
    runs_url = (
        f"{api}/actions/runs?event=pull_request&created={opened_date}..{merged_date}"
        "&per_page=100&page=1"
    )

    timeline_events, timeline_urls = _collect_list_pages(
        client=client,
        first_url=timeline_url,
        collection_name="timeline",
    )
    reviews, review_urls = _collect_list_pages(
        client=client,
        first_url=reviews_url,
        collection_name="reviews",
    )
    candidate_runs, run_urls = _collect_wrapped_pages(
        client=client,
        first_url=runs_url,
        field="workflow_runs",
    )
    workflow_runs = [
        run for run in candidate_runs if _workflow_run_belongs_to_pr(run, pr_number=pr_number)
    ]

    workflow_jobs: list[Mapping[str, Any]] = []
    job_urls: list[str] = []
    for run in workflow_runs:
        run_id = _positive_int(run.get("id"), name="workflow run id")
        jobs_url = f"{api}/actions/runs/{run_id}/jobs?filter=all&per_page=100&page=1"
        jobs, urls = _collect_wrapped_pages(
            client=client,
            first_url=jobs_url,
            field="jobs",
        )
        workflow_jobs.extend(jobs)
        job_urls.extend(urls)

    manifest = GitHubPRAcquisitionManifestV2(
        repository=repository,
        pr_number=pr_number,
        captured_at=captured_at,
        pull_request=GitHubEndpointCaptureV2(
            endpoint="pull_request",
            pages_fetched=1,
            record_count=1,
            complete=True,
            source_urls=(pr_page.source_url,),
        ),
        timeline=_capture("timeline", timeline_events, timeline_urls),
        reviews=_capture("reviews", reviews, review_urls),
        # ``record_count`` here is the deterministic PR-correlated subset of the
        # exhaustively fetched candidate pages identified by ``source_urls``.
        workflow_runs=_capture("workflow_runs", workflow_runs, run_urls),
        workflow_jobs=(
            _capture("workflow_jobs", workflow_jobs, job_urls)
            if workflow_runs
            else GitHubEndpointCaptureV2(
                endpoint="workflow_jobs",
                pages_fetched=0,
                record_count=0,
                complete=True,
                source_urls=(),
            )
        ),
    )
    return build_complete_github_pr_evidence_v2(
        manifest=manifest,
        pull_request=pull_request,
        timeline_events=timeline_events,
        reviews=reviews,
        workflow_runs=workflow_runs,
        workflow_jobs=workflow_jobs,
    )


def _capture(
    endpoint: str,
    records: Sequence[Mapping[str, Any]],
    source_urls: Sequence[str],
) -> GitHubEndpointCaptureV2:
    return GitHubEndpointCaptureV2(
        endpoint=endpoint,  # type: ignore[arg-type]
        pages_fetched=len(source_urls),
        record_count=len(records),
        complete=True,
        source_urls=tuple(source_urls),
    )


def _collect_list_pages(
    *,
    client: GitHubJsonClient,
    first_url: str,
    collection_name: str,
) -> tuple[list[Mapping[str, Any]], list[str]]:
    records: list[Mapping[str, Any]] = []
    source_urls: list[str] = []
    url: str | None = first_url
    seen: set[str] = set()
    while url is not None:
        if url in seen:
            raise ValueError(f"GitHub {collection_name} pagination cycle")
        seen.add(url)
        page = client.get_json(url)
        payload = page.payload
        if not isinstance(payload, list):
            raise ValueError(f"GitHub {collection_name} payload must be a list")
        records.extend(_require_mapping(record, name=collection_name) for record in payload)
        source_urls.append(page.source_url)
        url = page.next_url
    return records, source_urls


def _collect_wrapped_pages(
    *,
    client: GitHubJsonClient,
    first_url: str,
    field: str,
) -> tuple[list[Mapping[str, Any]], list[str]]:
    records: list[Mapping[str, Any]] = []
    source_urls: list[str] = []
    expected_total: int | None = None
    url: str | None = first_url
    seen: set[str] = set()
    while url is not None:
        if url in seen:
            raise ValueError(f"GitHub {field} pagination cycle")
        seen.add(url)
        page = client.get_json(url)
        payload = _require_mapping(page.payload, name=f"{field} envelope")
        collection = payload.get(field)
        if not isinstance(collection, list):
            raise ValueError(f"GitHub payload requires {field} list")
        page_total = _non_negative_int(payload.get("total_count"), name=f"{field} total_count")
        if expected_total is None:
            expected_total = page_total
        elif page_total != expected_total:
            raise ValueError(f"GitHub {field} total_count changed during pagination")
        records.extend(_require_mapping(record, name=field) for record in collection)
        source_urls.append(page.source_url)
        url = page.next_url
    if expected_total is None:
        raise ValueError(f"GitHub {field} acquisition fetched no pages")
    if len(records) != expected_total:
        raise ValueError(
            f"GitHub {field} total_count={expected_total} but acquired {len(records)} {field}"
        )
    return records, source_urls


def _workflow_run_belongs_to_pr(run: Mapping[str, Any], *, pr_number: int) -> bool:
    pull_requests = run.get("pull_requests")
    if not isinstance(pull_requests, list):
        return False
    return any(
        isinstance(pull_request, Mapping) and pull_request.get("number") == pr_number
        for pull_request in pull_requests
    )


def _iso_date(value: object, *, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"GitHub pull request requires {name}")
    normalized = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise ValueError(f"GitHub pull request requires ISO-8601 {name}") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"GitHub pull request requires timezone-aware {name}")
    return parsed.date().isoformat()


def _require_mapping(value: object, *, name: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError(f"GitHub {name} payload must be an object")
    return value


def _positive_int(value: object, *, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(f"GitHub {name} must be a positive integer")
    return value


def _non_negative_int(value: object, *, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"GitHub {name} must be a non-negative integer")
    return value


def _next_link(link_header: str | None) -> str | None:
    if not link_header:
        return None
    for part in link_header.split(","):
        section = part.strip()
        if 'rel="next"' not in section:
            continue
        if not section.startswith("<") or ">" not in section:
            raise ValueError("malformed GitHub Link header")
        return section[1 : section.index(">")]
    return None

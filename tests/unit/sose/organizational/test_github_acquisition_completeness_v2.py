from __future__ import annotations

from datetime import UTC, datetime

import pytest

from sose.organizational.github_acquisition_v2 import GitHubJsonPage, acquire_github_pr_v2


REPOSITORY = "brunolnetto/sose"


class FakeClient:
    def __init__(self, pages: dict[str, GitHubJsonPage]) -> None:
        self.pages = pages

    def get_json(self, url: str) -> GitHubJsonPage:
        return self.pages[url]


def test_wrapped_collection_total_count_must_match_materialized_records() -> None:
    api = f"https://api.github.com/repos/{REPOSITORY}"
    pr_url = f"{api}/pulls/302"
    timeline_url = f"{api}/issues/302/timeline?per_page=100&page=1"
    reviews_url = f"{api}/pulls/302/reviews?per_page=100&page=1"
    runs_url = (
        f"{api}/actions/runs?event=pull_request&created=2026-10-06..2026-10-06"
        "&per_page=100&page=1"
    )
    pages = {
        pr_url: GitHubJsonPage(
            source_url=pr_url,
            payload={
                "number": 302,
                "created_at": "2026-10-06T14:48:17Z",
                "merged_at": "2026-10-06T15:09:10Z",
                "url": pr_url,
                "base": {"repo": {"full_name": REPOSITORY}},
                "user": {"login": "brunolnetto", "type": "User"},
            },
        ),
        timeline_url: GitHubJsonPage(source_url=timeline_url, payload=[]),
        reviews_url: GitHubJsonPage(source_url=reviews_url, payload=[]),
        runs_url: GitHubJsonPage(
            source_url=runs_url,
            payload={
                "total_count": 2,
                "workflow_runs": [
                    {
                        "id": 10,
                        "workflow_id": 20,
                        "run_attempt": 1,
                        "pull_requests": [{"number": 302}],
                    }
                ],
            },
        ),
    }

    with pytest.raises(ValueError, match="total_count=2 but acquired 1 workflow_runs"):
        acquire_github_pr_v2(
            repository=REPOSITORY,
            pr_number=302,
            client=FakeClient(pages),
            captured_at=datetime(2026, 10, 6, 18, 0, tzinfo=UTC),
        )

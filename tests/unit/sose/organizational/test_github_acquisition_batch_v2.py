from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from sose.organizational.github_acquisition_batch_v2 import (
    acquire_github_pr_batch_v2,
    acquire_and_publish_github_pr_batch_v2,
)
from sose.organizational.github_acquisition_v2 import GitHubJsonPage


REPOSITORY = "brunolnetto/sose"
CAPTURED_AT = datetime(2026, 10, 6, 18, 30, tzinfo=UTC)


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


def _pages(numbers: tuple[int, ...]) -> dict[str, GitHubJsonPage]:
    pages: dict[str, GitHubJsonPage] = {}
    api = f"https://api.github.com/repos/{REPOSITORY}"
    for offset, number in enumerate(numbers):
        opened = datetime(2026, 10, 6, 14, 48, tzinfo=UTC) + timedelta(minutes=offset * 10)
        merged = opened + timedelta(minutes=5)
        pr_url = f"{api}/pulls/{number}"
        timeline_url = f"{api}/issues/{number}/timeline?per_page=100&page=1"
        reviews_url = f"{api}/pulls/{number}/reviews?per_page=100&page=1"
        runs_url = (
            f"{api}/actions/runs?event=pull_request"
            f"&created={opened.date().isoformat()}..{merged.date().isoformat()}"
            "&per_page=100&page=1"
        )
        pages[pr_url] = GitHubJsonPage(
            source_url=pr_url,
            payload={
                "number": number,
                "created_at": opened.isoformat().replace("+00:00", "Z"),
                "merged_at": merged.isoformat().replace("+00:00", "Z"),
                "url": pr_url,
                "base": {"repo": {"full_name": REPOSITORY}},
                "user": {"login": "brunolnetto", "type": "User"},
            },
        )
        pages[timeline_url] = GitHubJsonPage(source_url=timeline_url, payload=[])
        pages[reviews_url] = GitHubJsonPage(source_url=reviews_url, payload=[])
        pages[runs_url] = GitHubJsonPage(
            source_url=runs_url,
            payload={"total_count": 0, "workflow_runs": []},
        )
    return pages


def test_batch_acquisition_is_canonical_by_pr_creation_order() -> None:
    client = FakeClient(_pages((302, 303)))

    snapshot = acquire_github_pr_batch_v2(
        repository=REPOSITORY,
        pr_numbers=(303, 302),
        client=client,
        captured_at=CAPTURED_AT,
    )

    assert tuple(artifact.evidence.pr_number for artifact in snapshot.artifacts) == (302, 303)
    assert all(artifact.manifest.captured_at == CAPTURED_AT for artifact in snapshot.artifacts)
    assert len(snapshot.acquisition_hash) == 64


def test_batch_rejects_duplicate_or_too_small_pr_sets_before_network_access() -> None:
    client = FakeClient(_pages((302, 303)))

    with pytest.raises(ValueError, match="at least two distinct"):
        acquire_github_pr_batch_v2(
            repository=REPOSITORY,
            pr_numbers=(302, 302),
            client=client,
            captured_at=CAPTURED_AT,
        )
    assert client.requests == []

    with pytest.raises(ValueError, match="at least two distinct"):
        acquire_github_pr_batch_v2(
            repository=REPOSITORY,
            pr_numbers=(302,),
            client=client,
            captured_at=CAPTURED_AT,
        )
    assert client.requests == []


def test_batch_publication_is_canonical_and_idempotent(tmp_path: Path) -> None:
    output = tmp_path / "stage1-acquisition.json"
    client = FakeClient(_pages((302, 303)))

    first = acquire_and_publish_github_pr_batch_v2(
        repository=REPOSITORY,
        pr_numbers=(302, 303),
        client=client,
        captured_at=CAPTURED_AT,
        output_path=output,
    )
    repeated = acquire_and_publish_github_pr_batch_v2(
        repository=REPOSITORY,
        pr_numbers=(302, 303),
        client=FakeClient(_pages((302, 303))),
        captured_at=CAPTURED_AT,
        output_path=output,
    )

    assert repeated == first
    assert output.read_text(encoding="utf-8") == first.canonical_json() + "\n"


def test_batch_publication_never_replaces_different_snapshot(tmp_path: Path) -> None:
    output = tmp_path / "stage1-acquisition.json"
    acquire_and_publish_github_pr_batch_v2(
        repository=REPOSITORY,
        pr_numbers=(302, 303),
        client=FakeClient(_pages((302, 303, 304))),
        captured_at=CAPTURED_AT,
        output_path=output,
    )

    with pytest.raises(FileExistsError, match="different artifact"):
        acquire_and_publish_github_pr_batch_v2(
            repository=REPOSITORY,
            pr_numbers=(302, 303, 304),
            client=FakeClient(_pages((302, 303, 304))),
            captured_at=CAPTURED_AT,
            output_path=output,
        )


def test_failed_member_acquisition_publishes_nothing(tmp_path: Path) -> None:
    output = tmp_path / "stage1-acquisition.json"
    pages = _pages((302, 303))
    del pages[f"https://api.github.com/repos/{REPOSITORY}/pulls/303"]

    with pytest.raises(AssertionError, match="unexpected request"):
        acquire_and_publish_github_pr_batch_v2(
            repository=REPOSITORY,
            pr_numbers=(302, 303),
            client=FakeClient(pages),
            captured_at=CAPTURED_AT,
            output_path=output,
        )

    assert not output.exists()

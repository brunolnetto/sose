"""GitHub acquisition provenance: pagination and association fail-closed edge matrix."""
from __future__ import annotations

from datetime import UTC, datetime
from urllib.error import HTTPError

import pytest

from sose.organizational import github_acquisition_v2 as source


class Pages:
    def __init__(self, mapping):
        self.pages = mapping
        self.visited = []

    def get_json(self, url):
        self.visited.append(url)
        return self.pages[url]


def page(url, payload, next_url=None):
    return source.GitHubJsonPage(source_url=url, payload=payload, next_url=next_url)


def test_list_pages_preserve_provenance_and_detect_cycles_and_wrong_types():
    client = Pages({"first": page("first", [{"id": 1}], "second"),
                    "second": page("second", [{"id": 2}])})
    records, urls = source._collect_list_pages(client=client, first_url="first",
                                               collection_name="review")
    assert [record["id"] for record in records] == [1, 2]
    assert urls == ["first", "second"]
    client.pages["second"] = page("second", [], "first")
    with pytest.raises(ValueError, match="pagination cycle"):
        source._collect_list_pages(client=client, first_url="first",
                                   collection_name="review")
    client.pages["first"] = page("first", {"not": "a list"})
    with pytest.raises(ValueError, match="payload must be a list"):
        source._collect_list_pages(client=client, first_url="first",
                                   collection_name="review")
    client.pages["first"] = page("first", [None])
    with pytest.raises(ValueError, match="payload must be an object"):
        source._collect_list_pages(client=client, first_url="first",
                                   collection_name="review")


@pytest.mark.parametrize(("first", "second", "message"), [
    ({"total_count": 2, "jobs": [1]}, None, "payload must be an object"),
    ({"total_count": 2, "jobs": "bad"}, None, "requires jobs list"),
    ({"total_count": 2, "jobs": [{}]}, {"total_count": 3, "jobs": [{}]},
     "total_count changed"),
    ({"total_count": 2, "jobs": [{}]}, {"total_count": 2, "jobs": []},
     "total_count=2"),
    ({"total_count": 1, "jobs": [None]}, None, "payload must be an object"),
    ({"total_count": -1, "jobs": [{}]}, None, "must be a non-negative integer"),
])
def test_wrapped_pagination_rejects_malformed_and_lost_records(first, second, message):
    pages = {"first": page("first", first, "second" if second is not None else None)}
    if second is not None:
        pages["second"] = page("second", second)
    with pytest.raises(ValueError, match=message):
        source._collect_wrapped_pages(client=Pages(pages), first_url="first", field="jobs")


def test_wrapped_pagination_detects_repeat_without_ignoring_total():
    client = Pages({"first": page("first", {"total_count": 1, "jobs": [{}]}, "first")})
    with pytest.raises(ValueError, match="pagination cycle"):
        source._collect_wrapped_pages(client=client, first_url="first", field="jobs")


def test_datetime_integer_and_link_parsers_reject_ambiguous_source():
    for invalid in (None, "", "not-a-time", "2026-10-11T12:00:00"):
        with pytest.raises(ValueError, match="GitHub source requires"):
            source._iso_datetime(invalid, name="event_at")
    assert source._iso_datetime("2026-10-11T12:00:00Z", name="event_at").tzinfo is not None
    for invalid in (None, False, 0, -1, "3"):
        with pytest.raises(ValueError, match="positive integer"):
            source._positive_int(invalid, name="id")
    for invalid in (None, True, -1, "1"):
        with pytest.raises(ValueError, match="non-negative integer"):
            source._non_negative_int(invalid, name="count")
    assert source._positive_int(3, name="id") == 3
    assert source._non_negative_int(0, name="count") == 0
    assert source._next_link(None) is None
    assert source._next_link('<a>; rel="prev"') is None
    assert source._next_link('<https://api.github.com/next>; rel="next"') == "https://api.github.com/next"
    with pytest.raises(ValueError, match="malformed GitHub Link"):
        source._next_link('url; rel="next"')


def test_candidate_run_requires_matching_branch_repository_and_clock():
    opened = datetime(2026, 10, 11, 10, tzinfo=UTC)
    merged = datetime(2026, 10, 11, 12, tzinfo=UTC)
    valid = {"head_branch": "feature", "head_repository": {"id": 5, "full_name": "a/b"},
             "created_at": "2026-10-11T11:00:00Z"}
    pr = {"head": {"ref": "feature", "repo": {"id": 5, "full_name": "a/b"}}}
    def check(candidate, context):
        return source._workflow_run_matches_pr_candidate(
            candidate, pull_request=context, opened_at=opened, merged_at=merged,
        )
    assert check(valid, pr)
    assert not check(valid, {})
    assert not check(valid, {"head": {"ref": ""}})
    assert not check({**valid, "head_branch": "other"}, pr)
    assert not check({**valid, "head_repository": {"id": 6, "full_name": "a/b"}}, pr)
    assert not check({**valid, "head_repository": {"id": 5, "full_name": "else/where"}}, pr)
    assert not check({**valid, "created_at": "2026-10-11T09:00:00Z"}, pr)
    assert not check({**valid, "created_at": "2026-10-11T13:00:00Z"}, pr)


def test_repeated_commit_sha_association_is_queried_once_and_source_links_deduplicated():
    opened = datetime(2026, 10, 11, 10, tzinfo=UTC)
    merged = datetime(2026, 10, 11, 12, tzinfo=UTC)
    api = "https://api.github.com/repos/a/b"
    head_sha = "a" * 40
    url = api + "/commits/" + head_sha + "/pulls?per_page=100&page=1"
    client = Pages({url: page(url, [{"number": 7}])})
    candidate = {"head_branch": "feature", "head_sha": head_sha,
                 "head_repository": {"id": 5, "full_name": "a/b"},
                 "created_at": "2026-10-11T11:00:00Z", "pull_requests": []}
    expected, source_urls = source._correlate_workflow_runs_to_pr(
        client=client, api=api,
        pull_request={"head": {"ref": "feature", "repo": {"id": 5, "full_name": "a/b"}}},
        candidate_runs=[candidate, {**candidate, "id": 2}],
        pr_number=7, opened_at=opened, merged_at=merged,
    )
    assert len(expected) == 2
    assert source_urls == [url]
    assert client.visited == [url]


def test_commit_association_missing_head_sha_refuses_unfounded_PR_linkage():
    opened = datetime(2026, 10, 11, 10, tzinfo=UTC)
    merged = datetime(2026, 10, 11, 12, tzinfo=UTC)
    candidate = {"head_branch": "feature", "created_at": "2026-10-11T11:00:00Z",
                 "pull_requests": []}
    with pytest.raises(ValueError, match="requires head_sha"):
        source._correlate_workflow_runs_to_pr(
            client=Pages({}), api="https://api.github.com/repos/a/b",
            pull_request={"head": {"ref": "feature"}}, candidate_runs=[candidate],
            pr_number=7, opened_at=opened, merged_at=merged,
        )


def test_native_pull_request_association_rejects_non_list_and_non_matching_identity():
    assert not source._workflow_run_belongs_to_pr({}, pr_number=7)
    assert not source._workflow_run_belongs_to_pr(
        {"pull_requests": [None, {"number": 8}]}, pr_number=7,
    )
    assert source._workflow_run_belongs_to_pr(
        {"pull_requests": [{"number": 7}]}, pr_number=7,
    )


def test_urllib_github_client_preserves_headers_link_and_http_failure(monkeypatch):
    seen = []
    class Response:
        headers = {"Link": '<https://api.github.com/next>; rel="next"'}
        def __enter__(self):
            return self
        def __exit__(self, *_):
            return None
        def read(self):
            return b'{"id":7}'
        def geturl(self):
            return "https://api.github.com/final"

    def fake_urlopen(request):
        seen.append(request)
        return Response()

    monkeypatch.setattr(source, "urlopen", fake_urlopen)
    client = source.UrllibGitHubJsonClient(token="token", user_agent="audit")
    artifact = client.get_json("https://api.github.com/start")
    assert artifact.source_url == "https://api.github.com/final"
    assert artifact.payload == {"id": 7}
    assert artifact.next_url == "https://api.github.com/next"
    assert seen[0].get_header("Authorization") == "Bearer token"
    assert seen[0].get_header("User-agent") == "audit"

    def bad_urlopen(request):
        raise HTTPError(request.full_url, 403, "forbidden", None, None)
    monkeypatch.setattr(source, "urlopen", bad_urlopen)
    with pytest.raises(RuntimeError, match="HTTP 403"):
        client.get_json("https://api.github.com/start")

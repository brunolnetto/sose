from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone

import pytest

from sose.organizational.acquisition_v2 import (
    GitHubEndpointCaptureV2,
    GitHubPRAcquisitionManifestV2,
    build_complete_github_pr_evidence_v2,
    build_evidence_snapshot_from_acquisitions_v2,
)


def _capture(endpoint: str, *, count: int, complete: bool = True) -> GitHubEndpointCaptureV2:
    return GitHubEndpointCaptureV2(
        endpoint=endpoint,
        pages_fetched=1,
        record_count=count,
        complete=complete,
        source_urls=(f"https://api.github.com/example/{endpoint}",),
        next_page_url=None if complete else f"https://api.github.com/example/{endpoint}?page=2",
    )


def _manifest(
    *,
    pr_number: int = 302,
    timeline: int = 0,
    reviews: int = 0,
    runs: int = 0,
    jobs: int = 0,
    timeline_complete: bool = True,
    captured_at: datetime | None = None,
) -> GitHubPRAcquisitionManifestV2:
    return GitHubPRAcquisitionManifestV2(
        repository="brunolnetto/sose",
        pr_number=pr_number,
        captured_at=captured_at or datetime(2026, 10, 6, 16, 30, tzinfo=UTC),
        pull_request=_capture("pull_request", count=1),
        timeline=_capture("timeline", count=timeline, complete=timeline_complete),
        reviews=_capture("reviews", count=reviews),
        workflow_runs=_capture("workflow_runs", count=runs),
        workflow_jobs=_capture("workflow_jobs", count=jobs),
    )


def _pull_request(number: int = 302) -> dict[str, object]:
    return {
        "number": number,
        "created_at": "2026-10-06T14:48:17Z",
        "merged_at": "2026-10-06T15:09:10Z",
        "url": f"https://api.github.com/repos/brunolnetto/sose/pulls/{number}",
        "base": {"repo": {"full_name": "brunolnetto/sose"}},
        "user": {"login": "brunolnetto", "type": "User"},
    }


def test_complete_empty_endpoint_collections_are_distinct_from_missing_acquisition() -> None:
    artifact = build_complete_github_pr_evidence_v2(
        manifest=_manifest(),
        pull_request=_pull_request(),
        timeline_events=(),
        reviews=(),
        workflow_runs=(),
        workflow_jobs=(),
    )

    assert artifact.evidence.pr_number == 302
    assert artifact.evidence.review_timeline == ()
    assert artifact.evidence.submitted_reviews == ()
    assert artifact.evidence.workflow_jobs == ()
    assert artifact.manifest.is_complete
    assert len(artifact.artifact_hash) == 64


def test_incomplete_endpoint_cannot_enter_prospective_evidence() -> None:
    with pytest.raises(ValueError, match="timeline acquisition is incomplete"):
        build_complete_github_pr_evidence_v2(
            manifest=_manifest(timeline_complete=False),
            pull_request=_pull_request(),
        )


def test_manifest_counts_must_match_raw_payload_counts() -> None:
    with pytest.raises(ValueError, match="reviews record_count=1 but received 0 payloads"):
        build_complete_github_pr_evidence_v2(
            manifest=_manifest(reviews=1),
            pull_request=_pull_request(),
            reviews=(),
        )


def test_manifest_identity_must_match_source_record() -> None:
    with pytest.raises(ValueError, match="manifest pull request identity does not match"):
        build_complete_github_pr_evidence_v2(
            manifest=_manifest(pr_number=303),
            pull_request=_pull_request(302),
        )


def test_manifest_repository_must_match_native_pull_request_provenance() -> None:
    pull_request = _pull_request()
    pull_request["base"] = {"repo": {"full_name": "other/project"}}

    with pytest.raises(ValueError, match="manifest repository does not match"):
        build_complete_github_pr_evidence_v2(
            manifest=_manifest(),
            pull_request=pull_request,
        )


def test_complete_capture_rejects_a_next_page_and_incomplete_requires_one() -> None:
    with pytest.raises(ValueError, match="complete acquisition cannot retain next_page_url"):
        GitHubEndpointCaptureV2(
            endpoint="reviews",
            pages_fetched=1,
            record_count=0,
            complete=True,
            source_urls=("https://api.github.com/example/reviews",),
            next_page_url="https://api.github.com/example/reviews?page=2",
        )

    with pytest.raises(ValueError, match="incomplete acquisition requires next_page_url"):
        GitHubEndpointCaptureV2(
            endpoint="reviews",
            pages_fetched=1,
            record_count=0,
            complete=False,
            source_urls=("https://api.github.com/example/reviews",),
        )


def test_capture_requires_source_identity_even_when_the_collection_is_empty() -> None:
    with pytest.raises(ValueError, match="source_urls must identify fetched pages"):
        GitHubEndpointCaptureV2(
            endpoint="reviews",
            pages_fetched=1,
            record_count=0,
            complete=True,
        )


def test_capture_source_url_order_does_not_change_manifest_identity() -> None:
    left = GitHubEndpointCaptureV2(
        endpoint="reviews",
        pages_fetched=2,
        record_count=2,
        complete=True,
        source_urls=("https://example.test/b", "https://example.test/a"),
    )
    right = GitHubEndpointCaptureV2(
        endpoint="reviews",
        pages_fetched=2,
        record_count=2,
        complete=True,
        source_urls=("https://example.test/a", "https://example.test/b"),
    )
    assert left.canonical_payload() == right.canonical_payload()


def test_manifest_canonicalizes_capture_instant_to_utc() -> None:
    utc = _manifest(captured_at=datetime(2026, 10, 6, 16, 30, tzinfo=UTC))
    local = _manifest(
        captured_at=datetime(
            2026,
            10,
            6,
            13,
            30,
            tzinfo=timezone(timedelta(hours=-3)),
        )
    )

    assert utc.canonical_payload() == local.canonical_payload()


def test_snapshot_is_derived_only_from_complete_acquisition_artifacts() -> None:
    first = build_complete_github_pr_evidence_v2(
        manifest=_manifest(pr_number=302),
        pull_request=_pull_request(302),
    )
    second_manifest = _manifest(pr_number=303)
    second_pr = _pull_request(303)
    second_pr["created_at"] = "2026-10-06T14:54:57Z"
    second_pr["merged_at"] = "2026-10-06T15:16:26Z"
    second = build_complete_github_pr_evidence_v2(
        manifest=second_manifest,
        pull_request=second_pr,
    )

    snapshot = build_evidence_snapshot_from_acquisitions_v2((second, first))

    assert tuple(record.pr_number for record in snapshot.records) == (302, 303)
    assert len(snapshot.snapshot_hash) == 64

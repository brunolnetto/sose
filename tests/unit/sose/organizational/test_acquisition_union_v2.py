from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from sose.organizational.acquisition_v2 import (
    GitHubEndpointCaptureV2,
    GitHubPRAcquisitionArtifactV2,
    GitHubPRAcquisitionManifestV2,
    GitHubPRAcquisitionSnapshotV2,
)
from sose.organizational.acquisition_union_v2 import merge_acquisition_snapshots_v2
from sose.organizational.source_evidence_v2 import GitHubPREvidenceRecordV2


REPOSITORY = "brunolnetto/sose"
BASE = datetime(2026, 10, 6, 15, 0, tzinfo=UTC)


def test_union_preserves_prior_artifacts_and_canonicalizes_new_tranche() -> None:
    previous = GitHubPRAcquisitionSnapshotV2(artifacts=(_artifact(302), _artifact(303)))
    tranche = GitHubPRAcquisitionSnapshotV2(artifacts=(_artifact(305), _artifact(304)))

    merged = merge_acquisition_snapshots_v2(previous, tranche)

    assert tuple(item.evidence.pr_number for item in merged.artifacts) == (302, 303, 304, 305)
    assert merged.artifacts[:2] == previous.artifacts
    assert merged.to_evidence_snapshot().records[:2] == previous.to_evidence_snapshot().records


def test_identical_overlap_is_idempotent() -> None:
    previous = GitHubPRAcquisitionSnapshotV2(artifacts=(_artifact(302), _artifact(303)))
    overlapping = GitHubPRAcquisitionSnapshotV2(artifacts=(_artifact(303), _artifact(304)))

    merged = merge_acquisition_snapshots_v2(previous, overlapping)
    repeated = merge_acquisition_snapshots_v2(merged, overlapping)

    assert tuple(item.evidence.pr_number for item in merged.artifacts) == (302, 303, 304)
    assert repeated == merged
    assert repeated.acquisition_hash == merged.acquisition_hash


def test_conflicting_overlap_is_rejected_without_rewriting_prior_evidence() -> None:
    previous = GitHubPRAcquisitionSnapshotV2(artifacts=(_artifact(302), _artifact(303)))
    conflicting = GitHubPRAcquisitionSnapshotV2(
        artifacts=(_artifact(303, captured_offset=5), _artifact(304))
    )

    with pytest.raises(ValueError, match="cannot rewrite previously acquired pull request"):
        merge_acquisition_snapshots_v2(previous, conflicting)


def test_union_requires_same_repository() -> None:
    previous = GitHubPRAcquisitionSnapshotV2(artifacts=(_artifact(302), _artifact(303)))
    foreign = GitHubPRAcquisitionSnapshotV2(
        artifacts=(_artifact(304, repository="other/repo"), _artifact(305, repository="other/repo"))
    )

    with pytest.raises(ValueError, match="same repository"):
        merge_acquisition_snapshots_v2(previous, foreign)


def _artifact(
    number: int,
    *,
    repository: str = REPOSITORY,
    captured_offset: int = 0,
) -> GitHubPRAcquisitionArtifactV2:
    opened = BASE + timedelta(minutes=number - 300)
    captured = opened + timedelta(hours=1, minutes=captured_offset)
    api = f"https://api.github.com/repos/{repository}"

    def page(endpoint: str, count: int = 0) -> GitHubEndpointCaptureV2:
        return GitHubEndpointCaptureV2(
            endpoint=endpoint,
            pages_fetched=1,
            record_count=count,
            complete=True,
            source_urls=(f"{api}/{endpoint}/{number}?page=1",),
        )

    manifest = GitHubPRAcquisitionManifestV2(
        repository=repository,
        pr_number=number,
        captured_at=captured,
        pull_request=page("pull_request", 1),
        timeline=page("timeline"),
        reviews=page("reviews"),
        workflow_runs=page("workflow_runs"),
        workflow_jobs=GitHubEndpointCaptureV2(
            endpoint="workflow_jobs",
            pages_fetched=0,
            record_count=0,
            complete=True,
        ),
    )
    evidence = GitHubPREvidenceRecordV2(
        repository=repository,
        pr_number=number,
        opened_at=opened,
        merged_at=opened + timedelta(minutes=10),
        source_url=f"{api}/pulls/{number}",
    )
    return GitHubPRAcquisitionArtifactV2(manifest=manifest, evidence=evidence)

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from sose.organizational.acquisition_v2 import (
    GitHubEndpointCaptureV2,
    GitHubPRAcquisitionArtifactV2,
    GitHubPRAcquisitionManifestV2,
    GitHubPRAcquisitionSnapshotV2,
)
from sose.organizational.prospective_protocol_v2 import ProspectiveStudyProtocolV2
from sose.organizational.prospective_stage1_runner_v2 import (
    advance_stage1_artifacts_v2,
    run_stage1_tranche_files_v2,
)
from sose.organizational.source_evidence_v2 import GitHubPREvidenceRecordV2


REPOSITORY = "brunolnetto/sose"
REGISTERED_AT = datetime(2026, 10, 6, 14, 44, 13, tzinfo=UTC)


def test_initial_stage1_tranche_builds_state_and_readiness_without_freeze() -> None:
    protocol = _protocol()
    tranche = _snapshot(302, 303)

    result = advance_stage1_artifacts_v2(protocol=protocol, tranche=tranche)

    assert result.acquisition == tranche
    assert result.state.cohort.training_keys == ((REPOSITORY, 302), (REPOSITORY, 303))
    assert result.state.previous_state_hash is None
    assert result.checkpoint.training_observed == 2
    assert result.checkpoint.training_remaining == 16
    assert result.checkpoint.freeze_allowed is False
    assert result.checkpoint.holdout_exposed is False


def test_append_stage1_tranche_preserves_prior_evidence_and_chains_state() -> None:
    protocol = _protocol()
    first = advance_stage1_artifacts_v2(protocol=protocol, tranche=_snapshot(302, 303))

    second = advance_stage1_artifacts_v2(
        protocol=protocol,
        previous_acquisition=first.acquisition,
        tranche=_snapshot(304, 305),
        previous_state=first.state,
    )

    assert tuple(a.evidence.pr_number for a in second.acquisition.artifacts) == (302, 303, 304, 305)
    assert second.acquisition.artifacts[:2] == first.acquisition.artifacts
    assert second.state.previous_state_hash == first.state.state_hash
    assert second.checkpoint.training_observed == 4
    assert second.checkpoint.training_remaining == 14


def test_runner_publishes_three_idempotent_artifacts(tmp_path: Path) -> None:
    protocol = _protocol()
    protocol_path = tmp_path / "protocol.json"
    tranche_path = tmp_path / "tranche.json"
    acquisition_output = tmp_path / "cumulative.json"
    state_output = tmp_path / "state.json"
    checkpoint_output = tmp_path / "readiness.json"
    protocol_path.write_text(protocol.canonical_json() + "\n", encoding="utf-8")
    tranche_path.write_text(_snapshot(302, 303).canonical_json() + "\n", encoding="utf-8")

    first = run_stage1_tranche_files_v2(
        protocol_path=protocol_path,
        tranche_acquisition_path=tranche_path,
        cumulative_acquisition_output_path=acquisition_output,
        state_output_path=state_output,
        checkpoint_output_path=checkpoint_output,
    )
    repeated = run_stage1_tranche_files_v2(
        protocol_path=protocol_path,
        tranche_acquisition_path=tranche_path,
        cumulative_acquisition_output_path=acquisition_output,
        state_output_path=state_output,
        checkpoint_output_path=checkpoint_output,
    )

    assert repeated == first
    assert acquisition_output.read_text(encoding="utf-8") == first.acquisition.canonical_json() + "\n"
    assert state_output.read_text(encoding="utf-8") == first.state.canonical_json() + "\n"
    assert checkpoint_output.read_text(encoding="utf-8") == first.checkpoint.canonical_json() + "\n"


def test_runner_preflights_all_output_conflicts_before_publishing(tmp_path: Path) -> None:
    protocol_path = tmp_path / "protocol.json"
    tranche_path = tmp_path / "tranche.json"
    acquisition_output = tmp_path / "cumulative.json"
    state_output = tmp_path / "state.json"
    checkpoint_output = tmp_path / "readiness.json"
    protocol_path.write_text(_protocol().canonical_json() + "\n", encoding="utf-8")
    tranche_path.write_text(_snapshot(302, 303).canonical_json() + "\n", encoding="utf-8")
    state_output.write_text("different\n", encoding="utf-8")

    with pytest.raises(FileExistsError, match="different artifact"):
        run_stage1_tranche_files_v2(
            protocol_path=protocol_path,
            tranche_acquisition_path=tranche_path,
            cumulative_acquisition_output_path=acquisition_output,
            state_output_path=state_output,
            checkpoint_output_path=checkpoint_output,
        )

    assert not acquisition_output.exists()
    assert state_output.read_text(encoding="utf-8") == "different\n"
    assert not checkpoint_output.exists()


def _protocol() -> ProspectiveStudyProtocolV2:
    return ProspectiveStudyProtocolV2(
        protocol_document_hash="a" * 64,
        registration_merged_at=REGISTERED_AT,
    )


def _snapshot(*numbers: int) -> GitHubPRAcquisitionSnapshotV2:
    return GitHubPRAcquisitionSnapshotV2(artifacts=tuple(_artifact(number) for number in numbers))


def _artifact(number: int) -> GitHubPRAcquisitionArtifactV2:
    opened = REGISTERED_AT + timedelta(minutes=number - 300)
    merged = opened + timedelta(minutes=5)
    api = f"https://api.github.com/repos/{REPOSITORY}"

    def capture(endpoint: str, count: int = 0) -> GitHubEndpointCaptureV2:
        return GitHubEndpointCaptureV2(
            endpoint=endpoint,
            pages_fetched=1,
            record_count=count,
            complete=True,
            source_urls=(f"{api}/{endpoint}/{number}?page=1",),
        )

    manifest = GitHubPRAcquisitionManifestV2(
        repository=REPOSITORY,
        pr_number=number,
        captured_at=merged + timedelta(minutes=1),
        pull_request=capture("pull_request", 1),
        timeline=capture("timeline"),
        reviews=capture("reviews"),
        workflow_runs=capture("workflow_runs"),
        workflow_jobs=GitHubEndpointCaptureV2(
            endpoint="workflow_jobs",
            pages_fetched=0,
            record_count=0,
            complete=True,
        ),
    )
    evidence = GitHubPREvidenceRecordV2(
        repository=REPOSITORY,
        pr_number=number,
        opened_at=opened,
        merged_at=merged,
        source_url=f"{api}/pulls/{number}",
    )
    return GitHubPRAcquisitionArtifactV2(manifest=manifest, evidence=evidence)

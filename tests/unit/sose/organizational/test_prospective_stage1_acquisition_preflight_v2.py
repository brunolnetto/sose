from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from pydantic import ValidationError

from sose.organizational.acquisition_v2 import (
    GitHubEndpointCaptureV2,
    GitHubPRAcquisitionArtifactV2,
    GitHubPRAcquisitionManifestV2,
    GitHubPRAcquisitionSnapshotV2,
)
from sose.organizational.prospective_protocol_v2 import ProspectiveStudyProtocolV2
from sose.organizational.prospective_stage1_acquisition_runner_v2 import run_stage1_acquisition_v2
from sose.organizational.prospective_stage1_runner_v2 import advance_stage1_artifacts_v2
from sose.organizational.source_evidence_v2 import GitHubPREvidenceRecordV2


REPOSITORY = "brunolnetto/sose"
REGISTERED_AT = datetime(2026, 10, 6, 14, 44, 13, tzinfo=UTC)


class _FakeClient:
    pass


def test_malformed_previous_artifact_fails_before_network_acquisition(tmp_path: Path) -> None:
    protocol_document = tmp_path / "protocol-document.json"
    protocol_document.write_text("{}", encoding="utf-8")
    previous_acquisition = tmp_path / "previous-acquisition.json"
    previous_state = tmp_path / "previous-state.json"
    previous_acquisition.write_text("{not-json", encoding="utf-8")
    valid = advance_stage1_artifacts_v2(protocol=_protocol(), tranche=_snapshot(302, 303))
    previous_state.write_text(valid.state.canonical_json() + "\n", encoding="utf-8")
    acquired = False

    def acquire(**_: object) -> object:
        nonlocal acquired
        acquired = True
        return object()

    with pytest.raises(ValidationError):
        run_stage1_acquisition_v2(
            repository=REPOSITORY,
            pr_numbers=(304, 305),
            captured_at=datetime(2026, 10, 6, 19, 0, tzinfo=UTC),
            protocol_document_path=protocol_document,
            registration_merged_at=REGISTERED_AT,
            output_dir=tmp_path / "out",
            client=_FakeClient(),
            previous_acquisition_path=previous_acquisition,
            previous_state_path=previous_state,
            bind_protocol=_bind,
            acquire_batch=acquire,
            advance_files=lambda **_: object(),
        )

    assert acquired is False


def test_incompatible_previous_pair_fails_before_network_acquisition(tmp_path: Path) -> None:
    protocol_document = tmp_path / "protocol-document.json"
    protocol_document.write_text("{}", encoding="utf-8")
    first = advance_stage1_artifacts_v2(protocol=_protocol(), tranche=_snapshot(302, 303))
    unrelated = advance_stage1_artifacts_v2(protocol=_protocol(), tranche=_snapshot(304, 305))
    previous_acquisition = tmp_path / "previous-acquisition.json"
    previous_state = tmp_path / "previous-state.json"
    previous_acquisition.write_text(first.acquisition.canonical_json() + "\n", encoding="utf-8")
    previous_state.write_text(unrelated.state.canonical_json() + "\n", encoding="utf-8")
    acquired = False

    def acquire(**_: object) -> object:
        nonlocal acquired
        acquired = True
        return object()

    with pytest.raises(ValueError, match="previous acquisition and state"):
        run_stage1_acquisition_v2(
            repository=REPOSITORY,
            pr_numbers=(306, 307),
            captured_at=datetime(2026, 10, 6, 19, 0, tzinfo=UTC),
            protocol_document_path=protocol_document,
            registration_merged_at=REGISTERED_AT,
            output_dir=tmp_path / "out",
            client=_FakeClient(),
            previous_acquisition_path=previous_acquisition,
            previous_state_path=previous_state,
            bind_protocol=_bind,
            acquire_batch=acquire,
            advance_files=lambda **_: object(),
        )

    assert acquired is False


def _bind(**kwargs: object) -> object:
    Path(kwargs["output_path"]).write_text("{}\n", encoding="utf-8")
    return object()


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

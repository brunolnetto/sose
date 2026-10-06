from __future__ import annotations

from datetime import UTC, datetime, timedelta
import json
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
from sose.organizational.prospective_runner_v2 import (
    publish_prospective_protocol_binding_v2,
    run_prospective_evidence_files_v2,
)
from sose.organizational.prospective_state_v2 import ProspectiveEvidenceStateV2
from sose.organizational.source_evidence_v2 import (
    GitHubPREvidenceRecordV2,
    GitHubPREvidenceSnapshotV2,
)


REGISTERED_AT = datetime(2026, 10, 6, 14, 44, 13, tzinfo=UTC)
REPOSITORY = "brunolnetto/sose"


def test_publish_protocol_binding_is_canonical_and_idempotent(tmp_path: Path) -> None:
    document_path = tmp_path / "protocol.json"
    output_path = tmp_path / "protocol-binding.json"
    document_path.write_text(json.dumps(_document(), indent=2), encoding="utf-8")

    binding = publish_prospective_protocol_binding_v2(
        protocol_document_path=document_path,
        registration_merged_at=REGISTERED_AT,
        output_path=output_path,
    )
    repeated = publish_prospective_protocol_binding_v2(
        protocol_document_path=document_path,
        registration_merged_at=REGISTERED_AT,
        output_path=output_path,
    )

    restored = ProspectiveStudyProtocolV2.model_validate_json(output_path.read_text(encoding="utf-8"))
    assert repeated == binding
    assert restored == binding
    assert output_path.read_text(encoding="utf-8") == binding.canonical_json() + "\n"


def test_protocol_binding_refuses_to_replace_different_artifact(tmp_path: Path) -> None:
    document_path = tmp_path / "protocol.json"
    output_path = tmp_path / "protocol-binding.json"
    document_path.write_text(json.dumps(_document()), encoding="utf-8")
    publish_prospective_protocol_binding_v2(
        protocol_document_path=document_path,
        registration_merged_at=REGISTERED_AT,
        output_path=output_path,
    )

    changed = _document()
    changed["note"] = "different frozen artifact"
    document_path.write_text(json.dumps(changed), encoding="utf-8")

    with pytest.raises(FileExistsError, match="different artifact"):
        publish_prospective_protocol_binding_v2(
            protocol_document_path=document_path,
            registration_merged_at=REGISTERED_AT,
            output_path=output_path,
        )


def test_run_initial_prospective_state_from_complete_acquisition_artifacts(tmp_path: Path) -> None:
    protocol_path = _publish_protocol(tmp_path)
    acquisition_path = _write_acquisition_snapshot(tmp_path / "acquisition-1.json", (302, 303))
    output_path = tmp_path / "state-1.json"

    state = run_prospective_evidence_files_v2(
        protocol_path=protocol_path,
        acquisition_path=acquisition_path,
        output_path=output_path,
    )

    restored = ProspectiveEvidenceStateV2.model_validate_json(output_path.read_text(encoding="utf-8"))
    assert restored == state
    assert state.previous_state_hash is None
    assert state.cohort.training_keys == ((REPOSITORY, 302), (REPOSITORY, 303))
    assert state.protocol_hash == ProspectiveStudyProtocolV2.model_validate_json(
        protocol_path.read_text(encoding="utf-8")
    ).protocol_hash
    assert output_path.read_text(encoding="utf-8") == state.canonical_json() + "\n"


def test_runner_rejects_legacy_evidence_snapshot_without_acquisition_proof(tmp_path: Path) -> None:
    protocol_path = _publish_protocol(tmp_path)
    legacy_path = tmp_path / "legacy-snapshot.json"
    legacy = GitHubPREvidenceSnapshotV2(records=(_record(302, minute=5), _record(303, minute=15)))
    legacy_path.write_text(legacy.canonical_json() + "\n", encoding="utf-8")

    with pytest.raises(ValidationError):
        run_prospective_evidence_files_v2(
            protocol_path=protocol_path,
            acquisition_path=legacy_path,
            output_path=tmp_path / "state.json",
        )


def test_run_advance_chains_previous_state_without_rewriting_it(tmp_path: Path) -> None:
    protocol_path = _publish_protocol(tmp_path)
    first_acquisition = _write_acquisition_snapshot(tmp_path / "acquisition-1.json", (302, 303))
    first_path = tmp_path / "state-1.json"
    first = run_prospective_evidence_files_v2(
        protocol_path=protocol_path,
        acquisition_path=first_acquisition,
        output_path=first_path,
    )
    original_bytes = first_path.read_bytes()

    second_acquisition = _write_acquisition_snapshot(
        tmp_path / "acquisition-2.json", (302, 303, 304)
    )
    second_path = tmp_path / "state-2.json"
    second = run_prospective_evidence_files_v2(
        protocol_path=protocol_path,
        acquisition_path=second_acquisition,
        previous_state_path=first_path,
        output_path=second_path,
    )

    assert second.previous_state_hash == first.state_hash
    assert second.cohort.training_keys[-1] == (REPOSITORY, 304)
    assert first_path.read_bytes() == original_bytes


def test_run_rejects_previous_state_from_different_protocol(tmp_path: Path) -> None:
    protocol_path = _publish_protocol(tmp_path)
    first_acquisition = _write_acquisition_snapshot(tmp_path / "acquisition-1.json", (302, 303))
    first_path = tmp_path / "state-1.json"
    run_prospective_evidence_files_v2(
        protocol_path=protocol_path,
        acquisition_path=first_acquisition,
        output_path=first_path,
    )

    changed_document = _document()
    changed_document["note"] = "new protocol artifact"
    changed_document_path = tmp_path / "protocol-changed.json"
    changed_binding_path = tmp_path / "protocol-binding-changed.json"
    changed_document_path.write_text(json.dumps(changed_document), encoding="utf-8")
    publish_prospective_protocol_binding_v2(
        protocol_document_path=changed_document_path,
        registration_merged_at=REGISTERED_AT,
        output_path=changed_binding_path,
    )

    with pytest.raises(ValueError, match="protocol identity does not match"):
        run_prospective_evidence_files_v2(
            protocol_path=changed_binding_path,
            acquisition_path=_write_acquisition_snapshot(
                tmp_path / "acquisition-2.json", (302, 303, 304)
            ),
            previous_state_path=first_path,
            output_path=tmp_path / "state-2.json",
        )


def test_state_publication_refuses_to_overwrite_different_result(tmp_path: Path) -> None:
    protocol_path = _publish_protocol(tmp_path)
    output_path = tmp_path / "state.json"
    run_prospective_evidence_files_v2(
        protocol_path=protocol_path,
        acquisition_path=_write_acquisition_snapshot(
            tmp_path / "acquisition-1.json", (302, 303)
        ),
        output_path=output_path,
    )

    with pytest.raises(FileExistsError, match="different artifact"):
        run_prospective_evidence_files_v2(
            protocol_path=protocol_path,
            acquisition_path=_write_acquisition_snapshot(
                tmp_path / "acquisition-2.json", (302, 303, 304)
            ),
            output_path=output_path,
        )


def _publish_protocol(tmp_path: Path) -> Path:
    document_path = tmp_path / "protocol.json"
    binding_path = tmp_path / "protocol-binding.json"
    document_path.write_text(json.dumps(_document()), encoding="utf-8")
    publish_prospective_protocol_binding_v2(
        protocol_document_path=document_path,
        registration_merged_at=REGISTERED_AT,
        output_path=binding_path,
    )
    return binding_path


def _write_acquisition_snapshot(path: Path, numbers: tuple[int, ...]) -> Path:
    acquisition = GitHubPRAcquisitionSnapshotV2(
        artifacts=tuple(
            _artifact(number, minute=index * 10 + 5) for index, number in enumerate(numbers)
        )
    )
    path.write_text(acquisition.canonical_json() + "\n", encoding="utf-8")
    return path


def _artifact(number: int, *, minute: int) -> GitHubPRAcquisitionArtifactV2:
    record = _record(number, minute=minute)
    source = f"https://api.github.com/repos/{REPOSITORY}/pulls/{number}"

    def capture(endpoint: str) -> GitHubEndpointCaptureV2:
        return GitHubEndpointCaptureV2(
            endpoint=endpoint,
            pages_fetched=1,
            record_count=1 if endpoint == "pull_request" else 0,
            complete=True,
            source_urls=(f"{source}/{endpoint}",),
        )

    manifest = GitHubPRAcquisitionManifestV2(
        repository=REPOSITORY,
        pr_number=number,
        captured_at=record.merged_at + timedelta(seconds=1),
        pull_request=capture("pull_request"),
        timeline=capture("timeline"),
        reviews=capture("reviews"),
        workflow_runs=capture("workflow_runs"),
        workflow_jobs=capture("workflow_jobs"),
    )
    return GitHubPRAcquisitionArtifactV2(manifest=manifest, evidence=record)


def _record(number: int, *, minute: int) -> GitHubPREvidenceRecordV2:
    opened = REGISTERED_AT + timedelta(minutes=minute)
    return GitHubPREvidenceRecordV2(
        repository=REPOSITORY,
        pr_number=number,
        opened_at=opened,
        merged_at=opened + timedelta(minutes=2),
        source_url=f"https://api.github.com/repos/{REPOSITORY}/pulls/{number}",
    )


def _document() -> dict[str, object]:
    return {
        "protocol_version": "pr-review-validation/v2",
        "study_type": "prospective_two_stage",
        "repository": REPOSITORY,
        "registration_pr_number": 301,
        "activation_rule": "pull requests created strictly after registration PR merge",
        "cohort": {
            "training_count": 18,
            "holdout_count": 12,
            "selection": "first eligible merged pull requests in creation-time order",
        },
        "stage_rules": {
            "holdout_enrollment_before_model_freeze": False,
            "v2_model_may_use_training_only": True,
            "holdout_outcomes_may_change_model": False,
        },
    }

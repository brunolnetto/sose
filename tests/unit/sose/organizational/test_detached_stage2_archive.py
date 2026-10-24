"""Immutable Stage-2 historical evidence; never a prospective admission."""
from __future__ import annotations

import json
import shutil

import pytest

from sose.organizational.detached_stage2_archive import (
    _ARCHIVE,
    _git_blob_sha,
    verify_detached_stage2_archive,
)


def test_archived_stage2_evidence_matches_original_git_objects_and_is_incomplete():
    result = verify_detached_stage2_archive()
    assert result["git_blobs_verified"] == 15
    assert result["observed"] == 5
    assert result["target"] == 12
    assert result["validation_allowed"] is False
    assert result["status"] == "historical_incomplete_not_admitted"
    assert len(result["source_commit"]) == 40


def test_detached_verifier_rejects_byte_mutation_and_missing_checkpoint(tmp_path):
    dest = tmp_path / "archive"
    shutil.copytree(_ARCHIVE, dest)
    p = dest / "checkpoint-005/stage2-checkpoint.json"
    p.write_bytes(p.read_bytes() + b" ")
    with pytest.raises(ValueError, match="bytes differ"):
        verify_detached_stage2_archive(dest)
    shutil.copytree(_ARCHIVE, dest, dirs_exist_ok=True)
    (dest / "checkpoint-004/source.json").unlink()
    with pytest.raises(ValueError, match="missing"):
        verify_detached_stage2_archive(dest)


def test_detached_verifier_rejects_forged_admission(tmp_path):
    dest = tmp_path / "archive"
    shutil.copytree(_ARCHIVE, dest)
    manifest = dest / "manifest.json"
    data = json.loads(manifest.read_text())
    data["validation_allowed"] = True
    manifest.write_text(json.dumps(data))
    with pytest.raises(ValueError, match="manifest does not match"):
        verify_detached_stage2_archive(dest)


def test_detached_git_blob_identity_depends_on_full_bytes():
    assert _git_blob_sha(b"test") == "30d74d258442c7c65512eafab474568dd706c430"
    assert _git_blob_sha(b"test") != _git_blob_sha(b"test ")

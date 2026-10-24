"""#561: falsify detached Git evidence tampering without network or live credentials."""
from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path

import pytest

from scripts.verify_detached_stage2_evidence import verify_manifest


PREFIX = "docs/organizational/evidence/pr-review-v2-stage2-runtime/"


def _git(repo: Path, *args: str) -> str:
    return subprocess.check_output(["git", "-C", str(repo), *args], text=True).strip()


def _fixture(tmp_path: Path):
    repo = tmp_path / "archive"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "test@example.invalid")
    _git(repo, "config", "user.name", "Fixture")
    files = []
    for checkpoint in ("checkpoint-004", "checkpoint-005", "latest"):
        for name in ("cumulative-acquisition.json", "prospective-state.json",
                     "source.json", "stage2-checkpoint.json", "tranche-acquisition.json"):
            path = PREFIX + checkpoint + "/" + name
            payload = {"provenance": "fixture"}
            if name == "stage2-checkpoint.json":
                payload.update({"complete": False, "validation_allowed": False,
                                "holdout_observed": 5, "holdout_target": 12})
            contents = json.dumps(payload, sort_keys=True).encode()
            destination = repo / path
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(contents)
            sha = hashlib.sha1(b"blob " + str(len(contents)).encode() + b"\0" + contents).hexdigest()
            files.append({"path": path, "git_blob_sha1": sha, "size_bytes": len(contents)})
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "historical detached evidence")
    source = _git(repo, "rev-parse", "HEAD")
    manifest = {
        "schema_version": "sose-stage2-detached-evidence/v1",
        "source_commit": source,
        "scientific_status": "NOT_ADMITTED",
        "claimed_validation_allowed": False,
        "observed_holdout": 5, "required_holdout": 12,
        "files": sorted(files, key=lambda r: r["path"]),
    }
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(manifest))
    return repo, path, manifest


def test_exact_detached_source_matches_pinned_blob_identity(tmp_path):
    repo, path, _ = _fixture(tmp_path)
    report = verify_manifest(repo, path, expected_source_commit=_git(repo, "rev-parse", "HEAD"))
    assert report == {
        "source_commit": _git(repo, "rev-parse", "HEAD"),
        "verified_blobs": 15,
        "holdout_observed": 5, "holdout_required": 12,
        "validation_allowed": False,
    }


@pytest.mark.parametrize("tampering", ("hash", "size", "missing", "extra", "admission",
                                        "coverage", "alias", "source"))
def test_falsify_evidence_metadata_tampering(tmp_path, tampering):
    repo, path, manifest = _fixture(tmp_path)
    if tampering == "hash":
        manifest["files"][0]["git_blob_sha1"] = "a" * 40
    elif tampering == "size":
        manifest["files"][0]["size_bytes"] += 1
    elif tampering == "missing":
        manifest["files"].pop()
    elif tampering == "extra":
        manifest["files"].append(dict(manifest["files"][0]))
    elif tampering == "admission":
        manifest["claimed_validation_allowed"] = True
    elif tampering == "coverage":
        manifest["observed_holdout"] = 12
    elif tampering == "alias":
        alias = next(r for r in manifest["files"] if "/latest/" in r["path"])
        alias["git_blob_sha1"] = "0" * 40
    elif tampering == "source":
        manifest["source_commit"] = "0" * 40
    path.write_text(json.dumps(manifest))
    with pytest.raises((ValueError, subprocess.CalledProcessError)):
        verify_manifest(repo, path, expected_source_commit=_git(repo, "rev-parse", "HEAD"))


def test_falsify_new_unregistered_source_file(tmp_path):
    repo, path, _ = _fixture(tmp_path)
    extra = repo / (PREFIX + "checkpoint-005/extra.json")
    extra.write_text("{}")
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "source with unregistered extra")
    manifest = json.loads(path.read_text())
    manifest["source_commit"] = _git(repo, "rev-parse", "HEAD")
    path.write_text(json.dumps(manifest))
    with pytest.raises(ValueError, match="unregistered"):
        verify_manifest(repo, path, expected_source_commit=_git(repo, "rev-parse", "HEAD"))


def test_reject_self_consistent_replacement_commit_and_manifest(tmp_path):
    """Changing all rows and source SHA coherently cannot replace the trusted root."""
    first_dir = tmp_path / "first"
    replacement_dir = tmp_path / "replacement"
    first_dir.mkdir()
    replacement_dir.mkdir()
    original_repo, original_path, _ = _fixture(first_dir)
    replacement_repo, replacement_path, _ = _fixture(replacement_dir)
    trusted_commit = _git(original_repo, "rev-parse", "HEAD")

    # Even a valid but *different* source+manifest pair is not the accepted evidence.
    alternate = replacement_repo / (PREFIX + "checkpoint-004/source.json")
    alternate.write_text('{"provenance": "substituted"}')
    _git(replacement_repo, "add", ".")
    _git(replacement_repo, "commit", "-qm", "alternative source")
    replacement_manifest = json.loads(replacement_path.read_text())
    replacement_manifest["source_commit"] = _git(replacement_repo, "rev-parse", "HEAD")
    # Reconstruct a self-consistent blob table, not just one wrong sha.
    entries = _git(replacement_repo, "ls-tree", "-r", "--name-only", "HEAD").splitlines()
    for row in replacement_manifest["files"]:
        data = (replacement_repo / row["path"]).read_bytes()
        row["git_blob_sha1"] = hashlib.sha1(
            b"blob " + str(len(data)).encode() + b"\0" + data
        ).hexdigest()
        row["size_bytes"] = len(data)
    assert len(entries) == 15
    replacement_path.write_text(json.dumps(replacement_manifest))

    assert verify_manifest(
        replacement_repo, replacement_path,
        expected_source_commit=replacement_manifest["source_commit"]
    )["verified_blobs"] == 15
    with pytest.raises(ValueError, match="independently pinned source"):
        verify_manifest(
            replacement_repo, replacement_path,
            expected_source_commit=trusted_commit,
        )

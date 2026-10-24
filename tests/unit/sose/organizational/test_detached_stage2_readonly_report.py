"""#561 E2: deterministic incomplete Stage-2 read-only reporting."""
from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path

import pytest

from scripts.report_detached_stage2_evidence import build_incomplete_stage2_report


PREFIX = "docs/organizational/evidence/pr-review-v2-stage2-runtime/"
NAMES = ("cumulative-acquisition.json", "prospective-state.json", "source.json",
         "stage2-checkpoint.json", "tranche-acquisition.json")


def _git(repo: Path, *args: str) -> str:
    return subprocess.check_output(["git", "-C", str(repo), *args], text=True).strip()


def _fixture(tmp_path: Path, *, break_chain=False, duplicate_key=False, change_protocol=False):
    repo = tmp_path / "source"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.name", "Archive fixture")
    _git(repo, "config", "user.email", "fixture@example.invalid")

    seed_keys = [["brunolnetto/sose", n] for n in range(330, 334)]
    report = {}
    for checkpoint, observed, remaining in (
        ("checkpoint-004", 4, 8),
        ("checkpoint-005", 5, 7),
        ("latest", 5, 7),
    ):
        state = {
            "checkpoint_version": "pr-review-prospective-stage2-checkpoint/v2",
            "complete": False,
            "validation_allowed": False,
            "holdout_observed": observed,
            "holdout_remaining": remaining,
            "holdout_target": 12,
            "holdout_keys": seed_keys + ([["brunolnetto/sose", 334]] if observed == 5 else []),
            "previous_state_hash": (
                "f" * 64 if observed == 4
                else ("9" * 64 if break_chain else "a" * 64)
            ),
            "state_hash": "a" * 64 if observed == 4 else "b" * 64,
            "protocol_hash": (
                "c" * 64 if observed == 4
                else ("0" * 64 if change_protocol else "c" * 64)
            ),
            "model_freeze_hash": "d" * 64,
        }
        if duplicate_key and observed == 5:
            state["holdout_keys"][-1] = state["holdout_keys"][0]
        for name in NAMES:
            payload = state if name == "stage2-checkpoint.json" else {"type": name}
            path = repo / PREFIX / checkpoint / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(payload, sort_keys=True))
            report[f"{checkpoint}/{name}"] = path
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "immutable synthetic evidence")
    source = _git(repo, "rev-parse", "HEAD")
    rows = []
    for part, path in sorted(report.items()):
        b = path.read_bytes()
        sha = hashlib.sha1(b"blob " + str(len(b)).encode() + b"\0" + b).hexdigest()
        rows.append({"path": PREFIX + part, "git_blob_sha1": sha, "size_bytes": len(b)})
    manifest = {
        "schema_version": "sose-stage2-detached-evidence/v1",
        "source_commit": source,
        "scientific_status": "NOT_ADMITTED",
        "claimed_validation_allowed": False,
        "observed_holdout": 5,
        "required_holdout": 12,
        "files": rows,
    }
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(json.dumps(manifest))
    return repo, manifest_path, source


def test_incomplete_report_reads_verified_historical_chain_and_never_promotes(tmp_path):
    repo, manifest, sha = _fixture(tmp_path)
    baseline = _git(repo, "status", "--porcelain")
    outcome = build_incomplete_stage2_report(
        repo, manifest, expected_source_commit=sha,
    )
    assert outcome["evidence_commit"] == sha
    assert outcome["verified_blobs"] == 15
    assert outcome["checkpoint_sequence"] == [4, 5]
    assert (outcome["holdout_observed"], outcome["holdout_target"], outcome["holdout_remaining"]) == (5, 12, 7)
    assert outcome["validation_allowed"] is False
    assert outcome["scientific_status"] == "NOT_ADMITTED"
    assert outcome["previous_state_hash"] == "a" * 64
    assert _git(repo, "status", "--porcelain") == baseline


@pytest.mark.parametrize("violation", ("break_chain", "duplicate_key", "change_protocol"))
def test_report_rejects_hash_link_duplicate_or_frozen_protocol_mutation(tmp_path, violation):
    repo, manifest, sha = _fixture(tmp_path, **{violation: True})
    # The source and 15-blob manifest are internally correct; only science-chain
    # constraints reject the otherwise valid source commit.
    with pytest.raises(ValueError):
        build_incomplete_stage2_report(
            repo, manifest, expected_source_commit=sha,
        )


def test_report_does_not_trust_manifest_as_historical_root(tmp_path):
    repo, manifest, _ = _fixture(tmp_path)
    with pytest.raises(ValueError, match="independently pinned source"):
        build_incomplete_stage2_report(repo, manifest)

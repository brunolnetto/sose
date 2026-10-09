"""Immutable protocol promotion gate for MRO v1; executes no worlds."""
from __future__ import annotations

from hashlib import sha256
import json
import subprocess
from pathlib import Path

from sose.organizational.mro_protocol import build_mro_official_plan_v1


HISTORICAL_SOURCE_SHA = "3c356fea6adba2d27f583d3a1469e5df1c0b651b"

FREEZE_DIR = Path("docs/organizational/preregistration/mro-experiment-v1")


def test_mro_frozen_artifacts_match_exact_executable_plan_and_sources():
    root = Path.cwd()
    manifest = json.loads((root / FREEZE_DIR / "freeze-manifest.json").read_text())
    protocol_artifact = json.loads((root / FREEZE_DIR / "protocol.json").read_text())
    plan_artifact = json.loads((root / FREEZE_DIR / "plan.json").read_text())
    executable = build_mro_official_plan_v1()

    assert manifest["domain"] == "mro"
    assert manifest["status"] == "frozen"
    assert manifest["official_execution_performed"] is False
    assert manifest["scientific_claims_authorized"] is False
    assert manifest["protocol_hash"] == executable.protocol.protocol_hash
    assert manifest["plan_hash"] == executable.plan_hash
    assert protocol_artifact == executable.protocol.canonical_payload()
    assert plan_artifact == executable.canonical_payload()
    assert manifest["runtime_tree_scope"] == "src/sose"
    assert manifest["runtime_tree_sha"] == subprocess.check_output(
        ["git", "rev-parse", f"{HISTORICAL_SOURCE_SHA}:src/sose"], text=True
    ).strip()
    assert manifest["uv_lock_git_blob_sha"] == subprocess.check_output(
        ["git", "rev-parse", f"{HISTORICAL_SOURCE_SHA}:uv.lock"], text=True
    ).strip()
    assert len(manifest["files"]) == 7
    assert len({*manifest["files"]}) == 7
    for name, expected_hash in manifest["files"].items():
        path = root / name
        assert path.is_file(), name
        original_bytes = subprocess.check_output(
            ["git", "show", f"{HISTORICAL_SOURCE_SHA}:{name}"]
        )
        assert sha256(original_bytes).hexdigest() == expected_hash, name

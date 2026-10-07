from __future__ import annotations

import hashlib
import json
from pathlib import Path


ROOT = Path("docs/organizational/evidence/pr-review-v2-stage2/checkpoint-003")


def test_archived_stage2_checkpoint_003_matches_manifest() -> None:
    manifest = json.loads((ROOT / "manifest.json").read_text(encoding="utf-8"))

    assert manifest["checkpoint_number"] == 3
    assert manifest["holdout_observed"] == 3
    assert manifest["holdout_target"] == 12
    assert manifest["validation_allowed"] is False

    for name, expected in manifest["files"].items():
        digest = hashlib.sha256((ROOT / name).read_bytes()).hexdigest()
        assert expected == f"sha256:{digest}"


def test_archived_stage2_checkpoint_003_preserves_frozen_chain() -> None:
    manifest = json.loads((ROOT / "manifest.json").read_text(encoding="utf-8"))
    checkpoint = json.loads((ROOT / "stage2-checkpoint.json").read_text(encoding="utf-8"))

    assert checkpoint["holdout_keys"] == [
        ["brunolnetto/sose", 330],
        ["brunolnetto/sose", 331],
        ["brunolnetto/sose", 332],
    ]
    assert checkpoint["model_freeze_hash"] == manifest["model_freeze_hash"]
    assert checkpoint["state_hash"] == manifest["state_hash"]
    assert checkpoint["snapshot_hash"] == manifest["snapshot_hash"]
    assert checkpoint["holdout_observed"] == 3
    assert checkpoint["holdout_remaining"] == 9
    assert checkpoint["post_holdout_count"] == 0
    assert checkpoint["validation_allowed"] is False

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from sose.organizational.manufacturing_official_run import (
    FREEZE_MANIFEST,
    execute_official,
    verify_freeze,
)


def _running_at_frozen_manufacturing_inputs() -> bool:
    root = Path.cwd()
    manifest = json.loads((root / FREEZE_MANIFEST).read_text(encoding="utf-8"))
    return all(
        (root / entry["path"]).is_file()
        and hashlib.sha256((root / entry["path"]).read_bytes()).hexdigest() == entry["sha256"]
        for entry in manifest["files"].values()
    )


def _require_frozen_checkout_for_positive_execution() -> None:
    if not _running_at_frozen_manufacturing_inputs():
        pytest.skip(
            "positive Manufacturing v1 official execution is tested in the "
            "isolated immutable historical-replay workflow; current HEAD "
            "must not masquerade as the frozen source"
        )


def test_current_head_rejects_historical_v1_when_frozen_sources_differ() -> None:
    if _running_at_frozen_manufacturing_inputs():
        return
    with pytest.raises(ValueError, match="frozen dependency changed"):
        verify_freeze(Path.cwd())


def test_freeze_accepts_repository_frozen_artifacts() -> None:
    _require_frozen_checkout_for_positive_execution()
    manifest = verify_freeze(Path.cwd())
    assert manifest["protocol_hash"] == "8983bef05b5a2803f0f32be510790aa6f7ea9c453333e4a79449b186b1281d0d"


def test_freeze_rejects_mutated_dependency_without_executing(tmp_path: Path) -> None:
    root = Path.cwd()
    original = json.loads((root / FREEZE_MANIFEST).read_text(encoding="utf-8"))
    (tmp_path / FREEZE_MANIFEST).parent.mkdir(parents=True)
    original["files"]["adapter"]["sha256"] = "0" * 64
    (tmp_path / FREEZE_MANIFEST).write_text(json.dumps(original), encoding="utf-8")
    with pytest.raises(ValueError, match="trusted PR #375 merge provenance"):
        verify_freeze(tmp_path)


def test_official_execution_writes_complete_immutable_evidence(tmp_path: Path) -> None:
    _require_frozen_checkout_for_positive_execution()
    output = tmp_path / "official-v1"
    manifest = execute_official(root=Path.cwd(), output_dir=output)
    assert manifest["frozen_plan_hash"] == "4b972a4d0b86560718bc7f2d50ebf89b5f729c892681768fe89bd72a181a8911"
    assert len(json.loads((output / "worlds.json").read_text())) == 18
    assert len(json.loads((output / "runs.json").read_text())) == 36
    assert len(json.loads((output / "effects.json").read_text())) == 84
    evidence = json.loads((output / "evidence.json").read_text())
    assert len(evidence) == 36
    assert all("payload" in entry and "evidence_hash" in entry for entry in evidence)
    assert (output / "evidence-manifest.json").is_file()
    with pytest.raises(FileExistsError, match="refusing to overwrite"):
        execute_official(root=Path.cwd(), output_dir=output)


def test_freeze_rejects_tampered_manifest_even_if_internal_hashes_are_updated(tmp_path: Path) -> None:
    original = json.loads((Path.cwd() / FREEZE_MANIFEST).read_text(encoding="utf-8"))
    (tmp_path / FREEZE_MANIFEST).parent.mkdir(parents=True)
    original["source_head_sha"] = "0" * 40
    (tmp_path / FREEZE_MANIFEST).write_text(json.dumps(original), encoding="utf-8")
    with pytest.raises(ValueError, match="trusted PR #375 merge provenance"):
        verify_freeze(tmp_path)

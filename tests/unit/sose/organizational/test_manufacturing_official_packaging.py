"""Exercise current-head evidence packaging without impersonating the frozen v1 run.

Positive scientific replay belongs to the historical checkout workflow. These
tests isolate serialization, integrity and atomic publication behavior using
typed synthetic stubs; they do not certify Manufacturing v1 scientific results.
"""
from __future__ import annotations

from hashlib import sha256
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic import BaseModel

from sose.organizational import manufacturing_official_run as official
from sose.organizational.domain_experiment_runtime import evidence_hash


class _Evidence(BaseModel):
    units: int


class _Canonical:
    def __init__(self, name: str) -> None:
        self.name = name

    def canonical_payload(self) -> dict[str, str]:
        return {"name": self.name}


def _stub_packaging_sources(monkeypatch: pytest.MonkeyPatch):
    """Stub science only; leave the production serializer and filesystem real."""
    observed = _Evidence(units=7)
    result = SimpleNamespace(
        manifest=SimpleNamespace(
            world_count=18,
            run_count=36,
            canonical_payload=lambda: {"world_count": 18, "run_count": 36},
        ),
        evidence=(
            SimpleNamespace(
                world_hash="world-1",
                replication=0,
                evidence=observed,
                evidence_hash=evidence_hash(observed),
            ),
        ),
        worlds=tuple(_Canonical(f"world-{i}") for i in range(18)),
        runs=tuple(_Canonical(f"run-{i}") for i in range(36)),
        references=(_Canonical("reference"),),
        assessments=(_Canonical("assessment"),),
        result_hash="synthetic-packaging-test-result",
    )
    report = SimpleNamespace(effects=(_Canonical("test-effect"),))
    monkeypatch.setattr(
        official, "verify_freeze",
        lambda root: {"plan_hash": "synthetic-plan", "protocol_hash": "synthetic-protocol"},
    )
    monkeypatch.setattr(official, "ManufacturingReferenceDomain", object)
    monkeypatch.setattr(
        official, "build_manufacturing_official_plan_v1", lambda reference: object(),
    )
    monkeypatch.setattr(official, "run_domain_experiment", lambda **kwargs: result)
    monkeypatch.setattr(
        official, "analyze_domain_experiment", lambda **kwargs: report,
    )
    return result


def test_packaging_publishes_complete_hash_verified_directory_once(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    _stub_packaging_sources(monkeypatch)
    destination = tmp_path / "package"
    manifest = official.execute_official(root=Path.cwd(), output_dir=destination)

    expected = {
        "evidence.json", "worlds.json", "runs.json", "references.json",
        "assessments.json", "effects.json", "result-manifest.json",
    }
    assert set(manifest["files"]) == expected
    assert manifest["frozen_plan_hash"] == "synthetic-plan"
    assert manifest["frozen_protocol_hash"] == "synthetic-protocol"
    assert manifest["result_hash"] == "synthetic-packaging-test-result"
    for filename, expected_digest in manifest["files"].items():
        assert sha256((destination / filename).read_bytes()).hexdigest() == expected_digest
    assert json.loads((destination / "evidence-manifest.json").read_text()) == manifest
    assert len(json.loads((destination / "worlds.json").read_text())) == 18
    assert len(json.loads((destination / "runs.json").read_text())) == 36
    assert json.loads((destination / "evidence.json").read_text())[0]["payload"] == {
        "units": 7,
    }
    assert not destination.with_name("package.staging").exists()

    with pytest.raises(FileExistsError, match="refusing to overwrite"):
        official.execute_official(root=Path.cwd(), output_dir=destination)


def test_packaging_rejects_invalid_scientific_result_cardinality(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    result = _stub_packaging_sources(monkeypatch)
    result.manifest.world_count = 17
    with pytest.raises(ValueError, match="cardinality"):
        official.execute_official(root=Path.cwd(), output_dir=tmp_path / "package")
    assert not (tmp_path / "package").exists()


def test_packaging_rejects_untyped_evidence(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    result = _stub_packaging_sources(monkeypatch)
    result.evidence[0].evidence = {"units": 7}
    with pytest.raises(TypeError, match="typed model"):
        official.execute_official(root=Path.cwd(), output_dir=tmp_path / "package")
    assert not (tmp_path / "package").exists()


def test_packaging_rejects_evidence_digest_conflict(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    result = _stub_packaging_sources(monkeypatch)
    result.evidence[0].evidence_hash = "0" * 64
    with pytest.raises(ValueError, match="recorded hash"):
        official.execute_official(root=Path.cwd(), output_dir=tmp_path / "package")
    assert not (tmp_path / "package").exists()


def test_packaging_preserves_existing_staging_directory(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    _stub_packaging_sources(monkeypatch)
    staging = tmp_path / "package.staging"
    staging.mkdir()
    (staging / "sentinel").write_text("pre-existing")
    with pytest.raises(FileExistsError, match="staging directory"):
        official.execute_official(root=Path.cwd(), output_dir=tmp_path / "package")
    assert (staging / "sentinel").read_text() == "pre-existing"
    assert not (tmp_path / "package").exists()


def test_packaging_never_promotes_partial_staging_after_io_failure(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    _stub_packaging_sources(monkeypatch)
    original_write_text = Path.write_text

    def fail_on_runs(self: Path, *args, **kwargs):
        if self.name == "runs.json":
            raise OSError("injected evidence write failure")
        return original_write_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "write_text", fail_on_runs)
    destination = tmp_path / "package"
    with pytest.raises(OSError, match="injected evidence write failure"):
        official.execute_official(root=Path.cwd(), output_dir=destination)
    assert not destination.exists()
    assert destination.with_name("package.staging").exists()

from __future__ import annotations

from hashlib import sha256
import json
from pathlib import Path

import pytest

from scripts.audit_organizational_evidence import CANONICAL_FILES, audit_bundle


def _write(root: Path, name: str, obj) -> None:
    (root / name).write_text(json.dumps(obj, sort_keys=True) + "\n", encoding="utf-8")


def _digest(root: Path, name: str) -> str:
    return sha256((root / name).read_bytes()).hexdigest()


def _seal(root: Path) -> None:
    evidence = {
        "domain": "reference",
        "result_hash": "result-1",
        "frozen_plan_hash": "plan-1",
        "frozen_protocol_hash": "protocol-1",
        "eligible_assessment_count": 1,
        "passed_assessment_count": 0,
        "eligible_effect_count": 1,
        "files": {n: _digest(root, n) for n in CANONICAL_FILES},
    }
    _write(root, "evidence-manifest.json", evidence)
    _write(root, "final-provenance-manifest.json", {
        "files": {n: _digest(root, n)
                  for n in [*CANONICAL_FILES, "evidence-manifest.json"]}
    })


@pytest.fixture
def evidence_dir(tmp_path: Path) -> Path:
    _write(tmp_path, "result-manifest.json", {
        "result_hash": "result-1", "plan_hash": "plan-1",
        "protocol_hash": "protocol-1", "world_count": 1, "run_count": 1,
    })
    _write(tmp_path, "worlds.json", [{"world_hash": "w1"}])
    _write(tmp_path, "runs.json", [{
        "world_hash": "w1", "replication": 0, "evidence_hash": "e1"
    }])
    _write(tmp_path, "evidence.json", [{
        "world_hash": "w1", "replication": 0, "evidence_hash": "e1", "payload": {}
    }])
    _write(tmp_path, "references.json", [])
    _write(tmp_path, "assessments.json", [{
        "eligible": True, "passed": False, "claim_id": "synthetic.invariant"
    }])
    _write(tmp_path, "effects.json", [{
        "eligible": True, "paired": True, "treatment_arm_id": "intervention",
        "metric_name": "delay", "mean_delta": 2.0, "design_index": 0,
        "paired_standard_error": 0.0,
    }])
    _seal(tmp_path)
    return tmp_path


def test_audit_accepts_truth_falsification_as_a_scientific_result(evidence_dir: Path):
    audit = audit_bundle(domain="reference", root=evidence_dir)
    assert audit["verified_canonical_files"] == 7
    assert audit["verified_final_files"] == 8
    assert audit["assessments"]["eligible"] == 1
    assert audit["assessments"]["passed"] == 0
    assert audit["assessments"]["failed"] == 1
    assert audit["contrasts"][0]["mean_delta"] == 2.0


def test_rejects_tampered_canonical_evidence(evidence_dir: Path):
    (evidence_dir / "runs.json").write_text("[]\n", encoding="utf-8")
    with pytest.raises(ValueError, match="hash mismatch"):
        audit_bundle(domain="reference", root=evidence_dir)


def test_rejects_tampered_final_provenance(evidence_dir: Path):
    _write(evidence_dir, "execution-source-head.txt", {"extra": "not listed"})
    with pytest.raises(ValueError, match="inventory"):
        audit_bundle(domain="reference", root=evidence_dir)


def test_rejects_unreconciled_run_evidence(evidence_dir: Path):
    runs = json.loads((evidence_dir / "runs.json").read_text())
    runs[0]["evidence_hash"] = "changed"
    _write(evidence_dir, "runs.json", runs)
    _seal(evidence_dir)
    with pytest.raises(ValueError, match="identities"):
        audit_bundle(domain="reference", root=evidence_dir)


def test_rejects_eligible_effect_count_mismatch(evidence_dir: Path):
    em = json.loads((evidence_dir / "evidence-manifest.json").read_text())
    em["eligible_effect_count"] = 5
    _write(evidence_dir, "evidence-manifest.json", em)
    final = json.loads((evidence_dir / "final-provenance-manifest.json").read_text())
    final["files"]["evidence-manifest.json"] = _digest(evidence_dir, "evidence-manifest.json")
    _write(evidence_dir, "final-provenance-manifest.json", final)
    with pytest.raises(ValueError, match="eligible effect"):
        audit_bundle(domain="reference", root=evidence_dir)

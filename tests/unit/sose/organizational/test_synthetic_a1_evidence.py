from __future__ import annotations

import hashlib
import json
from pathlib import Path

from sose.organizational.synthetic_a1_report import A1ScientificReportV1


ROOT = Path("docs/organizational/evidence/synthetic-a1-reference-v1")


def test_a1_scientific_evidence_matches_manifest_bytes_and_report_hash() -> None:
    manifest = json.loads((ROOT / "manifest.json").read_text(encoding="utf-8"))

    report_bytes = (ROOT / "report.json").read_bytes()
    summary_bytes = (ROOT / "summary.json").read_bytes()

    assert hashlib.sha256(report_bytes).hexdigest() == manifest["report_sha256"]
    assert hashlib.sha256(summary_bytes).hexdigest() == manifest["summary_sha256"]

    report = A1ScientificReportV1.model_validate_json(report_bytes)
    assert report.report_hash == manifest["report_hash"]
    assert report.protocol_hash == manifest["protocol_hash"]


def test_a1_scientific_evidence_preserves_corrected_claim_boundary() -> None:
    summary = json.loads((ROOT / "summary.json").read_text(encoding="utf-8"))

    assert summary["pair_count"] == 576
    assert summary["a0_stable_worlds"] == 20
    assert summary["a0_saturated_worlds"] == 16
    assert summary["a1_nominal_stable_worlds"] == 20
    assert summary["a1_adaptation_stabilized_worlds"] == 10
    assert summary["a1_saturated_worlds"] == 6
    assert summary["saturated_to_stable_worlds"] == 10
    assert summary["stable_to_saturated_worlds"] == 0

    assert summary["direction_eligible_count"] == 8
    assert summary["direction_ineligible_count"] == 16
    assert summary["direction_agreement_count"] == 8
    assert summary["direction_change_count"] == 0
    assert summary["direction_agreement_rate"] == 1.0

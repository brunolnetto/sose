"""Validate domain-neutral official SOSE evidence without importing frozen runtimes.

Three independently frozen experiment releases share this audit envelope,
regardless of their domain mechanics. Invalid hashes are integrity failures;
failed ground-truth assessments are *scientific findings*, not audit failures.
"""
from __future__ import annotations

import argparse
from collections import defaultdict
from hashlib import sha256
import json
from math import fsum
from pathlib import Path
from typing import Any


CANONICAL_FILES = frozenset({
    "worlds.json", "runs.json", "evidence.json", "references.json",
    "assessments.json", "effects.json", "result-manifest.json",
})


def _reject_constant(value: str) -> None:
    raise ValueError(f"non-finite JSON constant is prohibited: {value}")


def _read(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"), parse_constant=_reject_constant)


def _digest(path: Path) -> str:
    return sha256(path.read_bytes()).hexdigest()


def _validate_inventory(root: Path, manifest: dict[str, Any], *, final: bool) -> None:
    files = manifest["files"]
    if not isinstance(files, dict) or not files:
        raise ValueError("evidence manifest must contain a file inventory")
    actual = {p.name for p in root.iterdir() if p.is_file()}
    if final:
        expected = actual - {"final-provenance-manifest.json"}
        if set(files) != expected:
            raise ValueError("final-provenance inventory does not match released files")
    elif set(files) != CANONICAL_FILES:
        raise ValueError("scientific manifest must cover seven canonical files")
    for name, expected_hash in files.items():
        if not name or Path(name).name != name or "/" in name or "\\" in name:
            raise ValueError(f"unsafe evidence filename: {name!r}")
        path = root / name
        if not path.is_file() or _digest(path) != expected_hash:
            raise ValueError(f"scientific evidence hash mismatch: {name}")


def audit_bundle(*, domain: str, root: Path) -> dict[str, Any]:
    """Verify archive-extracted evidence and return reproducible, descriptive statistics."""
    if not domain or not domain.strip():
        raise ValueError("domain name must be nonempty")
    root = root.resolve(strict=True)
    if not root.is_dir():
        raise NotADirectoryError(root)
    evidence_manifest = _read(root / "evidence-manifest.json")
    final_manifest = _read(root / "final-provenance-manifest.json")
    _validate_inventory(root, evidence_manifest, final=False)
    _validate_inventory(root, final_manifest, final=True)

    result = _read(root / "result-manifest.json")
    if evidence_manifest.get("domain", domain) != domain:
        raise ValueError("evidence domain does not match requested domain")
    for key in ("result_hash", "plan_hash", "protocol_hash"):
        frozen_key = "frozen_" + key if key != "result_hash" else "result_hash"
        if result[key] != evidence_manifest[frozen_key]:
            raise ValueError(f"result and evidence manifest disagree about {key}")

    worlds = _read(root / "worlds.json")
    runs = _read(root / "runs.json")
    evidence = _read(root / "evidence.json")
    assessments = _read(root / "assessments.json")
    effects = _read(root / "effects.json")
    _read(root / "references.json")
    if len(worlds) != result["world_count"] or len(runs) != result["run_count"]:
        raise ValueError("record cardinalities disagree with result manifest")
    if len(evidence) != len(runs):
        raise ValueError("a typed raw evidence record is missing")
    recorded = {(e["world_hash"], e["replication"], e["evidence_hash"]) for e in evidence}
    executed = {(r["world_hash"], r["replication"], r["evidence_hash"]) for r in runs}
    if len(recorded) != len(evidence) or recorded != executed:
        raise ValueError("raw evidence identities do not reconcile with runs")
    world_ids = {w["world_hash"] for w in worlds}
    if not {r["world_hash"] for r in runs} <= world_ids:
        raise ValueError("run refers to a world absent from the manifest")

    eligible_assessments = [a for a in assessments if a["eligible"]]
    passed = sum(a["passed"] is True for a in eligible_assessments)
    failed = sum(a["passed"] is False for a in eligible_assessments)
    unresolved = len(eligible_assessments) - passed - failed
    if "eligible_assessment_count" in evidence_manifest:
        if evidence_manifest["eligible_assessment_count"] != len(eligible_assessments):
            raise ValueError("eligible assessment count does not reconcile")
        if evidence_manifest["passed_assessment_count"] != passed:
            raise ValueError("passed assessment count does not reconcile")

    eligible_effects = [e for e in effects if e["eligible"]]
    if "eligible_effect_count" in evidence_manifest:
        if evidence_manifest["eligible_effect_count"] != len(eligible_effects):
            raise ValueError("eligible effect count does not reconcile")
    grouped: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for e in eligible_effects:
        if not e["paired"]:
            raise ValueError("eligible intervention evidence must remain paired")
        grouped[(e["treatment_arm_id"], e["metric_name"])].append(e)
    contrasts = []
    for (arm, metric), values in sorted(grouped.items()):
        deltas = [float(e["mean_delta"]) for e in values]
        signs = {"positive" if x > 0 else "negative" if x < 0 else "zero" for x in deltas}
        contrasts.append({
            "arm": arm, "metric": metric,
            "design_points": len({e["design_index"] for e in values}),
            "mean_delta": fsum(deltas) / len(deltas),
            "observed_signs": sorted(signs),
            "all_paired_standard_errors_zero": all(e["paired_standard_error"] == 0 for e in values),
        })
    return {
        "domain": domain,
        "result_hash": result["result_hash"],
        "protocol_hash": result["protocol_hash"],
        "plan_hash": result["plan_hash"],
        "worlds": len(worlds), "runs": len(runs), "raw_evidence_records": len(evidence),
        "assessments": {
            "total": len(assessments),
            "eligible": len(eligible_assessments),
            "passed": passed, "failed": failed, "unresolved": unresolved,
        },
        "effects": {
            "total": len(effects), "eligible": len(eligible_effects),
            "ineligible": len(effects) - len(eligible_effects),
        },
        "contrasts": contrasts,
        "verified_canonical_files": len(CANONICAL_FILES),
        "verified_final_files": len(final_manifest["files"]),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bundle", action="append", required=True, metavar="DOMAIN=DIR")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    summary = []
    for item in args.bundle:
        if "=" not in item:
            parser.error("each --bundle must be DOMAIN=DIR")
        domain, path = item.split("=", 1)
        summary.append(audit_bundle(domain=domain, root=Path(path)))
    output = {
        "audited_domains": summary,
        "total_worlds": sum(x["worlds"] for x in summary),
        "total_runs": sum(x["runs"] for x in summary),
        "total_eligible_assessments": sum(x["assessments"]["eligible"] for x in summary),
        "total_passed_assessments": sum(x["assessments"]["passed"] for x in summary),
        "total_unresolved_assessments": sum(x["assessments"]["unresolved"] for x in summary),
        "total_eligible_effects": sum(x["effects"]["eligible"] for x in summary),
    }
    text = json.dumps(output, indent=2, sort_keys=True, allow_nan=False) + "\n"
    if args.output:
        if args.output.exists():
            raise FileExistsError(f"refusing to overwrite audit summary: {args.output}")
        args.output.write_text(text, encoding="utf-8")
    else:
        print(text, end="")


if __name__ == "__main__":
    main()

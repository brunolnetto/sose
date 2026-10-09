"""Run an independently frozen O2C/MRO synthetic experiment and seal evidence.

IMPORTANT: these are official synthetic mechanics tests, not empirical calibration.
This script lives outside src/sose so the runtime source Git tree stays frozen.
"""
from __future__ import annotations

import argparse
from hashlib import sha1, sha256
import json
from pathlib import Path
import subprocess

from pydantic import BaseModel

from sose.organizational.domain_experiment_analysis import analyze_domain_experiment
from sose.organizational.domain_experiment_runtime import evidence_hash, run_domain_experiment
from sose.organizational.freeze_candidate import DEPENDENCIES
from sose.organizational.o2c_protocol import build_o2c_official_plan_v1
from sose.organizational.o2c_reference import O2CReferenceDomain
from sose.organizational.mro_protocol import build_mro_official_plan_v1
from sose.organizational.mro_reference import MROReferenceDomain


# Exact frozen-manifest Git blob identities from separately reviewed freeze PRs.
# A changed manifest must be approved and this script updated before any rerun.
ANCHORS = {
    "o2c": "5c60565eb576afee0ff8bb16cfe938b8052e1bd5",
    "mro": "a6ff93510f8ff460ccf7430d89baa6c4d52cdc9b",
}
REFERENCES = {
    "o2c": (O2CReferenceDomain, build_o2c_official_plan_v1, 12, 24),
    "mro": (MROReferenceDomain, build_mro_official_plan_v1, 18, 36),
}


def _digest(path: Path) -> str:
    return sha256(path.read_bytes()).hexdigest()


def _git_blob(data: bytes) -> str:
    return sha1(b"blob " + str(len(data)).encode("ascii") + bytes([0]) + data).hexdigest()


def _git_object(root: Path, path: str) -> str:
    return subprocess.check_output(
        ["git", "rev-parse", f"HEAD:{path}"], cwd=root, text=True
    ).strip()


def verify_freeze(*, root: Path, domain: str):
    """Reject any changes to scientific input, runtime source tree or uv.lock."""
    if domain not in REFERENCES:
        raise ValueError(f"unsupported official domain: {domain}")
    path = root / f"docs/organizational/preregistration/{domain}-experiment-v1"
    data = (path / "freeze-manifest.json").read_bytes()
    if _git_blob(data) != ANCHORS[domain]:
        raise ValueError(f"{domain} freeze manifest differs from approved immutable anchor")
    manifest = json.loads(data)
    if (
        manifest["domain"] != domain
        or manifest["status"] != "frozen"
        or manifest["official_execution_performed"] is not False
    ):
        raise ValueError("invalid scientific freeze state")
    if set(manifest["files"]) != set(DEPENDENCIES[domain]):
        raise ValueError("frozen source inventory differs from expected contract")
    for name, expected in manifest["files"].items():
        file_path = root / name
        if not file_path.is_file() or _digest(file_path) != expected:
            raise ValueError(f"frozen scientific source drift: {name}")
    if _git_object(root, "src/sose") != manifest["runtime_tree_sha"]:
        raise ValueError("frozen transitive executable runtime Git tree has changed")
    if _git_object(root, "uv.lock") != manifest["uv_lock_git_blob_sha"]:
        raise ValueError("frozen uv.lock Git blob has changed")

    reference_cls, plan_builder, _, _ = REFERENCES[domain]
    plan = plan_builder(reference_cls())
    if plan.protocol.protocol_hash != manifest["protocol_hash"]:
        raise ValueError("executable scientific protocol hash differs from frozen protocol")
    if plan.plan_hash != manifest["plan_hash"]:
        raise ValueError("executable scientific plan hash differs from frozen plan")
    if json.loads((path / "protocol.json").read_text()) != plan.protocol.canonical_payload():
        raise ValueError("protocol artifact differs from executable frozen protocol")
    if json.loads((path / "plan.json").read_text()) != plan.canonical_payload():
        raise ValueError("plan artifact differs from executable frozen plan")
    return manifest


def run_official(*, root: Path, domain: str, output_dir: Path) -> dict[str, object]:
    """Verify everything first; then execute and atomically publish canonical evidence."""
    frozen = verify_freeze(root=root, domain=domain)
    if output_dir.exists():
        raise FileExistsError(f"refusing to overwrite official evidence: {output_dir}")
    reference_cls, plan_builder, expected_worlds, expected_runs = REFERENCES[domain]
    reference = reference_cls()
    plan = plan_builder(reference)
    result = run_domain_experiment(reference=reference, plan=plan)
    report = analyze_domain_experiment(reference=reference, result=result)
    if (result.manifest.world_count, result.manifest.run_count) != (
        expected_worlds, expected_runs
    ):
        raise ValueError("official experiment cardinality does not match frozen plan")
    eligible_assessments = [a for a in result.assessments if a.eligible]
    if not eligible_assessments:
        raise ValueError("no eligible ground truth assessed in official experiment")
    raw = []
    for record in result.evidence:
        if not isinstance(record.evidence, BaseModel):
            raise TypeError("raw evidence is not a typed Pydantic model")
        if evidence_hash(record.evidence) != record.evidence_hash:
            raise ValueError("raw evidence integrity violation")
        raw.append({
            "world_hash": record.world_hash,
            "replication": record.replication,
            "evidence_hash": record.evidence_hash,
            "payload": record.evidence.model_dump(mode="json"),
        })
    payloads = {
        "worlds.json": [x.canonical_payload() for x in result.worlds],
        "runs.json": [x.canonical_payload() for x in result.runs],
        "evidence.json": raw,
        "references.json": [x.canonical_payload() for x in result.references],
        "assessments.json": [x.canonical_payload() for x in result.assessments],
        "effects.json": [x.canonical_payload() for x in report.effects],
        "result-manifest.json": result.manifest.canonical_payload(),
    }
    staging = output_dir.with_name(output_dir.name + ".staging")
    if staging.exists():
        raise FileExistsError(f"existing unresolved staging evidence: {staging}")
    staging.mkdir(parents=True, exist_ok=False)
    for name, payload in payloads.items():
        (staging / name).write_text(
            json.dumps(payload, sort_keys=True, indent=2, ensure_ascii=False, allow_nan=False)
            + "\n",
            encoding="utf-8",
        )
    manifest = {
        "domain": domain,
        "frozen_protocol_hash": frozen["protocol_hash"],
        "frozen_plan_hash": frozen["plan_hash"],
        "frozen_manifest_git_blob": ANCHORS[domain],
        "frozen_runtime_tree_sha": frozen["runtime_tree_sha"],
        "frozen_uv_lock_git_blob": frozen["uv_lock_git_blob_sha"],
        "result_hash": result.result_hash,
        "eligible_assessment_count": len(eligible_assessments),
        "passed_assessment_count": sum(a.passed is True for a in eligible_assessments),
        "eligible_effect_count": sum(e.eligible for e in report.effects),
        "files": {name: _digest(staging / name) for name in sorted(payloads)},
    }
    (staging / "evidence-manifest.json").write_text(
        json.dumps(manifest, sort_keys=True, indent=2) + "\n", encoding="utf-8"
    )
    staging.rename(output_dir)
    return manifest


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--domain", required=True, choices=sorted(REFERENCES))
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    report = run_official(
        root=args.root.resolve(),
        domain=args.domain,
        output_dir=args.output_dir.resolve(),
    )
    print(json.dumps(report, sort_keys=True, indent=2))


if __name__ == "__main__":
    main()

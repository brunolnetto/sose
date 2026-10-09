"""Execute frozen Manufacturing v1 and export reproducible scientific evidence.

Run from the repository root:
    python -m sose.organizational.manufacturing_official_run --output-dir artifacts/manufacturing-v1

The output directory must not exist: official evidence is immutable once written.
"""
from __future__ import annotations

import argparse
from hashlib import sha1, sha256
import json
from pathlib import Path

from pydantic import BaseModel

from .domain_experiment_analysis import analyze_domain_experiment
from .domain_experiment_runtime import evidence_hash, run_domain_experiment
from .manufacturing_protocol import build_manufacturing_official_plan_v1
from .manufacturing_reference import ManufacturingReferenceDomain


FREEZE_DIR = Path("docs/organizational/preregistration")
FREEZE_MANIFEST = FREEZE_DIR / "manufacturing-experiment-v1-freeze-manifest.json"
# Git blob identity from the immutable squash-merge commit of freeze PR #375.
# Do not derive this value from the working tree or from the manifest itself.
FROZEN_MANIFEST_GIT_BLOB_SHA = "18468f3d7e79e1402cb493e399d955e9af2d055e"
FROZEN_MERGE_COMMIT = "8f6ef825a891b2d27ea77218c6fcb11147056eb4"


def _digest(path: Path) -> str:
    return sha256(path.read_bytes()).hexdigest()


def verify_freeze(root: Path) -> dict[str, object]:
    """Reject changed or incomplete frozen inputs before executing any world."""
    manifest_bytes = (root / FREEZE_MANIFEST).read_bytes()
    git_blob = sha1(b"blob " + str(len(manifest_bytes)).encode("ascii") + b"\\0" + manifest_bytes).hexdigest()
    if git_blob != FROZEN_MANIFEST_GIT_BLOB_SHA:
        raise ValueError("freeze manifest differs from trusted PR #375 merge provenance")
    manifest = json.loads(manifest_bytes)
    files = manifest["files"]
    if set(files) != {
        "adapter", "prd", "preregistration", "process_manifest",
        "protocol", "specification", "trd",
    }:
        raise ValueError("unexpected frozen dependency inventory")
    for label, entry in files.items():
        path = root / entry["path"]
        if not path.is_file() or _digest(path) != entry["sha256"]:
            raise ValueError(f"frozen dependency changed: {label}")
    prereg = json.loads((root / FREEZE_DIR / "manufacturing-experiment-v1.json").read_text(encoding="utf-8"))
    if prereg["status"] != "frozen":
        raise ValueError("Manufacturing preregistration is not frozen")
    plan = build_manufacturing_official_plan_v1()
    if plan.plan_hash != manifest["plan_hash"] or plan.protocol.protocol_hash != manifest["protocol_hash"]:
        raise ValueError("runtime plan differs from the frozen experiment")
    protocol_json = json.loads((root / FREEZE_DIR / "manufacturing-experiment-v1-protocol.json").read_text(encoding="utf-8"))
    if plan.protocol.canonical_payload() != protocol_json:
        raise ValueError("runtime protocol differs from the frozen artifact")
    return manifest


def execute_official(*, root: Path, output_dir: Path) -> dict[str, object]:
    """Validate first, execute precisely once, and write audit-ready records."""
    frozen = verify_freeze(root)
    if output_dir.exists():
        raise FileExistsError(f"refusing to overwrite official evidence: {output_dir}")
    reference = ManufacturingReferenceDomain()
    plan = build_manufacturing_official_plan_v1(reference)
    result = run_domain_experiment(reference=reference, plan=plan)
    report = analyze_domain_experiment(reference=reference, result=result)
    if result.manifest.world_count != 18 or result.manifest.run_count != 36:
        raise ValueError("official experiment cardinality deviates from preregistration")

    raw_evidence = []
    for record in result.evidence:
        if not isinstance(record.evidence, BaseModel):
            raise TypeError("Manufacturing raw evidence must be a typed model")
        if evidence_hash(record.evidence) != record.evidence_hash:
            raise ValueError("raw evidence payload does not match its recorded hash")
        raw_evidence.append({
            "world_hash": record.world_hash,
            "replication": record.replication,
            "evidence_hash": record.evidence_hash,
            "payload": record.evidence.model_dump(mode="json"),
        })
    payloads = {
        "evidence.json": raw_evidence,
        "worlds.json": [item.canonical_payload() for item in result.worlds],
        "runs.json": [item.canonical_payload() for item in result.runs],
        "references.json": [item.canonical_payload() for item in result.references],
        "assessments.json": [item.canonical_payload() for item in result.assessments],
        "effects.json": [item.canonical_payload() for item in report.effects],
        "result-manifest.json": result.manifest.canonical_payload(),
    }
    # Stage complete evidence before publishing the immutable directory.
    staging = output_dir.with_name(output_dir.name + ".staging")
    if staging.exists():
        raise FileExistsError(f"staging directory already exists: {staging}")
    staging.mkdir(parents=True, exist_ok=False)
    try:
        for filename, value in payloads.items():
            (staging / filename).write_text(
                json.dumps(value, sort_keys=True, indent=2, ensure_ascii=False, allow_nan=False) + "\n",
                encoding="utf-8",
            )
        manifest = {
            "freeze_manifest_sha256": _digest(root / FREEZE_MANIFEST),
            "frozen_plan_hash": frozen["plan_hash"],
            "frozen_protocol_hash": frozen["protocol_hash"],
            "result_hash": result.result_hash,
            "files": {name: _digest(staging / name) for name in sorted(payloads)},
        }
        (staging / "evidence-manifest.json").write_text(
            json.dumps(manifest, sort_keys=True, indent=2) + "\n", encoding="utf-8"
        )
        staging.rename(output_dir)
    except BaseException:
        # Incomplete evidence is never promoted. Preserve staging for investigation.
        raise
    return manifest


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    manifest = execute_official(root=args.root.resolve(), output_dir=args.output_dir.resolve())
    print(json.dumps(manifest, sort_keys=True, indent=2))


if __name__ == "__main__":
    main()

"""Read-only verification of the detached prospective Stage-2 evidence git tree.

Never mutates the source evidence or treats its partial holdout as validation.
Requires the exact historical commit to be available in the local Git object store.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
from pathlib import Path


def _git(repo: Path, *args: str) -> bytes:
    return subprocess.run(
        ["git", "-C", str(repo), *args], check=True, capture_output=True,
    ).stdout


# Independent of the manifest and its candidate file table. This is a source
# trust anchor approved with the archival gate, not a value read from evidence.
HISTORICAL_SOURCE_COMMIT = "f7bda7c51fecdf40d337e8bc8b850e4dc32b17d5"


def verify_manifest(
    repo: Path,
    manifest_path: Path,
    *,
    expected_source_commit: str = HISTORICAL_SOURCE_COMMIT,
) -> dict:
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("schema_version") != "sose-stage2-detached-evidence/v1":
        raise ValueError("unknown detached evidence manifest schema")
    if manifest.get("scientific_status") != "NOT_ADMITTED":
        raise ValueError("detached evidence must not be promoted to admission")
    if manifest.get("claimed_validation_allowed") is not False:
        raise ValueError("historic Stage-2 validation was not allowed")
    if type(manifest.get("observed_holdout")) is not int or type(manifest.get("required_holdout")) is not int:
        raise ValueError("holdout counts must be integers")
    if (manifest["observed_holdout"], manifest["required_holdout"]) != (5, 12):
        raise ValueError("source holdout coverage is 5 of 12")
    source = manifest.get("source_commit")
    if not isinstance(source, str) or len(source) != 40 or any(c not in "0123456789abcdef" for c in source):
        raise ValueError("invalid pinned source commit")
    if source != expected_source_commit:
        raise ValueError("manifest source_commit differs from independently pinned source")
    actual_commit = _git(repo, "rev-parse", "--verify", source + "^{commit}").decode().strip()
    if actual_commit != source:
        raise ValueError("pinned source commit mismatch")
    raw = _git(repo, "ls-tree", "-rz", "--full-tree", source)
    observed = {}
    for record in raw.split(b"\0"):
        if not record:
            continue
        metadata, filename = record.split(b"\t", 1)
        mode, kind, oid = metadata.decode().split()
        if mode != "100644" or kind != "blob":
            raise ValueError("detached evidence must contain only regular blobs")
        path = filename.decode("utf-8")
        if path in observed:
            raise ValueError("duplicate source tree path")
        observed[path] = oid

    records = manifest.get("files")
    if not isinstance(records, list) or len(records) != 15:
        raise ValueError("expected exactly 15 source evidence blobs")
    expected = {}
    for row in records:
        if not isinstance(row, dict) or set(row) != {"path", "git_blob_sha1", "size_bytes"}:
            raise ValueError("malformed evidence record")
        path, oid, size = row["path"], row["git_blob_sha1"], row["size_bytes"]
        prefix = "docs/organizational/evidence/pr-review-v2-stage2-runtime/"
        if not isinstance(path, str) or not path.startswith(prefix):
            raise ValueError("out-of-scope evidence path")
        parts = path[len(prefix):].split("/")
        if len(parts) != 2 or parts[0] not in {"checkpoint-004", "checkpoint-005", "latest"} or parts[1] not in {
            "cumulative-acquisition.json", "prospective-state.json", "source.json",
            "stage2-checkpoint.json", "tranche-acquisition.json",
        }:
            raise ValueError("unexpected evidence path")
        if path in expected or not isinstance(oid, str) or len(oid) != 40 or type(size) is not int or size < 0:
            raise ValueError("duplicate or malformed evidence blob")
        expected[path] = oid
        if observed.get(path) != oid:
            raise ValueError("manifest/source Git blob divergence: " + path)
        content = _git(repo, "cat-file", "blob", oid)
        computed = hashlib.sha1(b"blob " + str(len(content)).encode() + b"\0" + content).hexdigest()
        if computed != oid or len(content) != size:
            raise ValueError("evidence byte identity mismatch: " + path)
        json.loads(content)  # invalid JSON fails closed
    if observed != expected:
        raise ValueError("source tree has unregistered or missing files")
    for name in ("cumulative-acquisition.json", "prospective-state.json", "source.json",
                 "stage2-checkpoint.json", "tranche-acquisition.json"):
        root = "docs/organizational/evidence/pr-review-v2-stage2-runtime/"
        if expected[root + "latest/" + name] != expected[root + "checkpoint-005/" + name]:
            raise ValueError("latest alias no longer equals checkpoint-005")
    checkpoint_path = "docs/organizational/evidence/pr-review-v2-stage2-runtime/checkpoint-005/stage2-checkpoint.json"
    checkpoint = json.loads(_git(repo, "cat-file", "blob", expected[checkpoint_path]))
    if checkpoint.get("complete") is not False or checkpoint.get("validation_allowed") is not False:
        raise ValueError("detached evidence is NOT a completed validation")
    if checkpoint.get("holdout_observed") != 5 or checkpoint.get("holdout_target") != 12:
        raise ValueError("checkpoint does not support manifest holdout counts")
    return {"source_commit": source, "verified_blobs": len(records),
            "holdout_observed": 5, "holdout_required": 12,
            "validation_allowed": False}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repository", type=Path, default=Path("."))
    parser.add_argument("--manifest", type=Path, default=Path(
        "docs/organizational/evidence/archives/pr-review-v2-stage2-detached/manifest.json"))
    args = parser.parse_args()
    print(json.dumps(verify_manifest(args.repository, args.manifest), sort_keys=True))


if __name__ == "__main__":
    main()

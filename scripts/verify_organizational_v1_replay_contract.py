"""Verify immutable v1 freeze anchors and exact-source historical replay in current CI.

This current-source checker is independent of the official frozen runners.
It must fail closed before any historical world is executed.
"""
from __future__ import annotations

import argparse
from hashlib import sha1
import json
from pathlib import Path
import re

import yaml

PUBLISHED_PATH = Path(".github/organizational-v1-published-digests.json")
WORKFLOW_PATH = Path(".github/workflows/organizational-v1-evidence.yml")
ADR_PATH = "docs/architecture/decisions/adr-0007-versioned-scientific-freezes.md"
CHECKER_PATH = "scripts/verify_organizational_v1_replay_contract.py"
APPROVED_PUBLISHED_BLOB = "410ecb050eb3943997b184e36d6f70d73d40620c"
APPROVED = {
    "manufacturing": ("125fb707e5cbed05ac19e6d43bcd0736bd4b14f6",
                      "f79bd89fc50ca5fadc0788c657cc752d1291dd464fbf1643e660a67667c4338b"),
    "o2c": ("3c356fea6adba2d27f583d3a1469e5df1c0b651b",
            "c51013cb109f67f5e39bb87385f0b666632c2db953d3084d7dd6ce95adc6500c"),
    "mro": ("3c356fea6adba2d27f583d3a1469e5df1c0b651b",
            "7e23c5f230034c3fb4cf79786110332e9b8c6da299a18bd3245a56fc13228740"),
}
FREEZES = {
    "manufacturing": ("docs/organizational/preregistration/manufacturing-experiment-v1-freeze-manifest.json",
                      "18468f3d7e79e1402cb493e399d955e9af2d055e"),
    "o2c": ("docs/organizational/preregistration/o2c-experiment-v1/freeze-manifest.json",
            "5c60565eb576afee0ff8bb16cfe938b8052e1bd5"),
    "mro": ("docs/organizational/preregistration/mro-experiment-v1/freeze-manifest.json",
            "a6ff93510f8ff460ccf7430d89baa6c4d52cdc9b"),
}
TREE_SHA = "eeae0590f0ab84230a2cabfecd5225111d777592"
LOCK_BLOB = "5902ec2400ba5998f154831d080b1e5773da7413"
CANONICAL = {"assessments.json", "effects.json", "evidence.json",
             "references.json", "result-manifest.json", "runs.json", "worlds.json"}


def git_blob(data: bytes) -> str:
    return sha1(b"blob " + str(len(data)).encode("ascii") + bytes([0]) + data).hexdigest()


def _same(actual, expected, name: str) -> None:
    if actual != expected:
        raise ValueError("v1 governance mismatch: " + name)


def verify_registry(root: Path) -> dict:
    data = (root / PUBLISHED_PATH).read_bytes()
    _same(git_blob(data), APPROVED_PUBLISHED_BLOB, "trusted published file digests")
    records = json.loads(data)
    _same(set(records), set(APPROVED), "published domain set")
    for domain, (source, result) in APPROVED.items():
        item = records[domain]
        _same(item["source_sha"], source, domain + " historical source")
        _same(item["result_hash"], result, domain + " published result")
        digests = item["canonical_files"]
        _same(set(digests), CANONICAL, domain + " evidence file names")
        if any(not re.fullmatch("[0-9a-f]{64}", digest) for digest in digests.values()):
            raise ValueError(domain + " has malformed canonical digest")
    return records


def verify_manifests(root: Path) -> None:
    for domain, (path, anchor) in FREEZES.items():
        data = (root / path).read_bytes()
        _same(git_blob(data), anchor, domain + " immutable preregistration blob")
        manifest = json.loads(data)
        _same(manifest["domain"], domain, domain + " declared domain")
        _same(manifest["official_execution_performed"], False, domain + " freeze status")
        _same(len(manifest["files"]), 7, domain + " seven source artifacts")
        if domain == "manufacturing":
            _same(set(manifest["files"]),
                  {"adapter", "prd", "preregistration", "process_manifest",
                   "protocol", "specification", "trd"},
                  "Manufacturing selected-file inventory")
            if "runtime_tree_sha" in manifest or "uv_lock_git_blob_sha" in manifest:
                raise ValueError("Manufacturing did not preregister a whole-tree freeze")
        else:
            _same(manifest["runtime_tree_scope"], "src/sose", domain + " tree scope")
            _same(manifest["runtime_tree_sha"], TREE_SHA, domain + " source tree")
            _same(manifest["uv_lock_git_blob_sha"], LOCK_BLOB, domain + " uv lock")


def verify_workflow(root: Path, registry: dict) -> None:
    workflow = yaml.load((root / WORKFLOW_PATH).read_text(), Loader=yaml.BaseLoader)
    watched = {"src/sose/**", "scripts/**", "uv.lock", ADR_PATH,
               "tests/unit/sose/organizational/test_historical_replay_governance.py",
               str(WORKFLOW_PATH), str(PUBLISHED_PATH)}
    for event in ("push", "pull_request"):
        if not watched <= set(workflow["on"][event]["paths"]):
            raise ValueError("v1 replay is not triggered by governance edits")
    job = workflow["jobs"]["replay-historical-v1"]
    rows = job["strategy"]["matrix"]["include"]
    if len(rows) != len(APPROVED):
        raise ValueError("incomplete v1 historical replay matrix")
    seen = {}
    for row in rows:
        name = row["domain"]
        if name in seen:
            raise ValueError("duplicate historical replay domain")
        seen[name] = (row["source_sha"], row["result_sha"])
    _same(seen, APPROVED, "historical replay identity")
    for domain in seen:
        _same(seen[domain], (registry[domain]["source_sha"],
                            registry[domain]["result_hash"]),
              domain + " source versus published digests")
    steps = job["steps"]
    def step(name):
        found = [s for s in steps if s.get("name") == name]
        if len(found) != 1:
            raise ValueError("missing required replay step: " + name)
        return found[0]

    checkout = step("Checkout exact historical scientific source")
    if checkout["with"]["ref"] != "$" + "{{ matrix.source_sha }}":
        raise ValueError("historical replay is not pinned to source SHA")
    _same(checkout["with"]["path"], "historical-source", "historical checkout path")
    _same(step("Verify checkout identity")["working-directory"],
          "historical-source", "historical checkout assertion")
    evidence = step("Independently verify canonical historical result and file digests")
    _same(evidence["working-directory"], "historical-source", "hash verifier directory")
    for evidence_rule in ("published[\"canonical_files\"]", "hashlib.sha256",
                          "actual_digest == expected_digest",
                          "evidence[\"files\"][name] == expected_digest",
                          "result[\"result_hash\"] == expected"):
        if evidence_rule not in evidence["run"]:
            raise ValueError("missing independent file-digest verification")
    governance = step("Verify immutable historical freeze governance")
    if CHECKER_PATH not in governance["run"]:
        raise ValueError("current-source governance verifier is not invoked")
    execution = step("Re-execute original v1 without publishing as a new official experiment")
    if steps.index(governance) >= steps.index(execution):
        raise ValueError("governance must be checked before historical execution")
    if step("Keep evidence or interrupted staging for diagnosis").get("if") != "always()":
        raise ValueError("interrupted historical evidence must be retained")


def verify_governance(root: Path) -> dict:
    registry = verify_registry(root)
    verify_manifests(root)
    verify_workflow(root, registry)
    return {"protocol": "organizational-v1-governance/v1",
            "status": "pinned-source-and-released-digests-verified",
            "registry_blob": APPROVED_PUBLISHED_BLOB,
            "domains": sorted(registry)}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    report = verify_governance(args.root.resolve())
    rendered = json.dumps(report, sort_keys=True, indent=2) + "\n"
    if args.output is not None:
        args.output.write_text(rendered, encoding="utf-8")
    print(rendered, end="")


if __name__ == "__main__":
    main()

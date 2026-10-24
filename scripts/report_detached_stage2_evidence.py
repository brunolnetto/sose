"""Deterministic *read-only* report for the incomplete detached Stage-2 archive.

This is neither a scientific validation nor a release/admission mechanism. The
source is verified against its independent Git commit pin before any inspection.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from scripts.verify_detached_stage2_evidence import (
    HISTORICAL_SOURCE_COMMIT,
    _git,
    verify_manifest,
)

_PREFIX = "docs/organizational/evidence/pr-review-v2-stage2-runtime/"


def build_incomplete_stage2_report(
    repository: Path,
    manifest_path: Path,
    *,
    expected_source_commit: str = HISTORICAL_SOURCE_COMMIT,
) -> dict[str, object]:
    evidence = verify_manifest(
        repository, manifest_path, expected_source_commit=expected_source_commit
    )

    def read_checkpoint(part: str) -> dict:
        path = _PREFIX + part + "/stage2-checkpoint.json"
        data = json.loads(_git(repository, "show", f"{expected_source_commit}:{path}"))
        if not isinstance(data, dict):
            raise ValueError("checkpoint must be a JSON object")
        return data

    earlier = read_checkpoint("checkpoint-004")
    latest = read_checkpoint("checkpoint-005")
    if earlier.get("checkpoint_version") != "pr-review-prospective-stage2-checkpoint/v2":
        raise ValueError("unexpected historical checkpoint-004 version")
    if latest.get("checkpoint_version") != earlier["checkpoint_version"]:
        raise ValueError("changed historical checkpoint version")
    if earlier.get("complete") is not False or latest.get("complete") is not False:
        raise ValueError("incomplete source was falsely marked complete")
    if earlier.get("validation_allowed") is not False or latest.get("validation_allowed") is not False:
        raise ValueError("historical experiment cannot be promoted to validation")
    if (earlier.get("holdout_observed"), latest.get("holdout_observed")) != (4, 5):
        raise ValueError("unexpected historical acquisition counts")
    if (earlier.get("holdout_remaining"), latest.get("holdout_remaining")) != (8, 7):
        raise ValueError("unexpected historical remaining counts")
    if earlier.get("holdout_target") != 12 or latest.get("holdout_target") != 12:
        raise ValueError("inconsistent preregistered holdout size")
    before, after = earlier.get("holdout_keys"), latest.get("holdout_keys")
    if not isinstance(before, list) or not isinstance(after, list):
        raise ValueError("missing immutable holdout identifiers")
    if len(before) != 4 or len(after) != 5 or after[:4] != before:
        raise ValueError("checkpoint history must append exactly one eligible holdout")
    if len({tuple(x) for x in after}) != 5:
        raise ValueError("duplicate holdout key")
    if latest.get("previous_state_hash") != earlier.get("state_hash"):
        raise ValueError("historical Stage-2 state hash chain broken")
    if not all(
        isinstance(latest.get(name), str)
        and len(latest[name]) == 64
        and latest.get(name) == earlier.get(name)
        for name in ("protocol_hash", "model_freeze_hash")
    ):
        raise ValueError("protocol/model freeze identity changed between checkpoints")

    return {
        "evidence_commit": evidence["source_commit"],
        "verified_blobs": evidence["verified_blobs"],
        "scientific_status": "NOT_ADMITTED",
        "validation_allowed": False,
        "checkpoint_sequence": [4, 5],
        "holdout_observed": 5,
        "holdout_target": 12,
        "holdout_remaining": 7,
        "previous_state_hash": latest["previous_state_hash"],
        "state_hash": latest["state_hash"],
        "protocol_hash": latest["protocol_hash"],
        "model_freeze_hash": latest["model_freeze_hash"],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repository", type=Path, default=Path("."))
    parser.add_argument(
        "--manifest", type=Path,
        default=Path(
            "docs/organizational/evidence/archives/pr-review-v2-stage2-detached/manifest.json"
        ),
    )
    args = parser.parse_args()
    print(json.dumps(
        build_incomplete_stage2_report(args.repository, args.manifest),
        sort_keys=True, indent=2,
    ))


if __name__ == "__main__":
    main()

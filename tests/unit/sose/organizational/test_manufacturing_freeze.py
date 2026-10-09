from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path

from sose.organizational.manufacturing_protocol import (
    build_manufacturing_official_plan_v1,
    build_manufacturing_protocol_v1,
)


HISTORICAL_SOURCE_SHA = "125fb707e5cbed05ac19e6d43bcd0736bd4b14f6"
ROOT = Path("docs/organizational/preregistration")
PROTOCOL_PATH = ROOT / "manufacturing-experiment-v1-protocol.json"
PREREG_PATH = ROOT / "manufacturing-experiment-v1.json"
MANIFEST_PATH = ROOT / "manufacturing-experiment-v1-freeze-manifest.json"


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_manufacturing_v1_frozen_protocol_matches_executable_builder() -> None:
    frozen = json.loads(PROTOCOL_PATH.read_text(encoding="utf-8"))
    protocol = build_manufacturing_protocol_v1()

    assert frozen == protocol.canonical_payload()
    assert protocol.protocol_hash == json.loads(
        MANIFEST_PATH.read_text(encoding="utf-8")
    )["protocol_hash"]


def test_manufacturing_v1_freeze_manifest_binds_plan_and_preregistration() -> None:
    manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    prereg = json.loads(PREREG_PATH.read_text(encoding="utf-8"))
    plan = build_manufacturing_official_plan_v1()

    assert prereg["status"] == "frozen"
    assert prereg["protocol_artifact"]["status"] == "frozen"
    assert prereg["protocol_artifact"]["protocol_hash"] == plan.protocol.protocol_hash
    assert prereg["protocol_artifact"]["plan_hash"] == plan.plan_hash

    assert manifest["protocol_hash"] == plan.protocol.protocol_hash
    assert manifest["plan_hash"] == plan.plan_hash
    assert manifest["files"]["protocol"]["sha256"] == _sha256(PROTOCOL_PATH)
    assert manifest["files"]["preregistration"]["sha256"] == _sha256(PREREG_PATH)


def test_manufacturing_v1_freeze_preserves_no_execution_boundary() -> None:
    manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))

    assert manifest["official_execution_performed"] is False
    assert manifest["official_result_inspected"] is False


def test_manufacturing_v1_manifest_covers_every_frozen_dependency() -> None:
    """Detect changes in any preregistered artifact, not just the protocol JSON.

    The source head is provenance metadata, not a substitute for verifying the
    files that the frozen experiment actually depends on.
    """

    manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    artifacts = manifest["files"]
    assert set(artifacts) == {
        "adapter",
        "prd",
        "preregistration",
        "process_manifest",
        "protocol",
        "specification",
        "trd",
    }

    paths = [entry["path"] for entry in artifacts.values()]
    assert len(paths) == len(set(paths)), "freeze dependencies must be unique"
    for kind, entry in artifacts.items():
        artifact_path = Path(entry["path"])
        assert artifact_path.is_file(), f"missing frozen {kind}: {artifact_path}"
        frozen_bytes = subprocess.check_output(
            ["git", "show", f"{HISTORICAL_SOURCE_SHA}:{entry['path']}"]
        )
        assert hashlib.sha256(frozen_bytes).hexdigest() == entry["sha256"], (
            f"historical frozen {kind} has drifted: {artifact_path}"
        )

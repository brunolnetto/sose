"""Immutable protocol promotion gate for O2C v1; executes no worlds."""
from __future__ import annotations

from hashlib import sha256
import json
from pathlib import Path

from sose.organizational.o2c_protocol import build_o2c_official_plan_v1


FREEZE_DIR = Path("docs/organizational/preregistration/o2c-experiment-v1")


def test_o2c_frozen_artifacts_match_exact_executable_plan_and_sources():
    root = Path.cwd()
    manifest = json.loads((root / FREEZE_DIR / "freeze-manifest.json").read_text())
    protocol_artifact = json.loads((root / FREEZE_DIR / "protocol.json").read_text())
    plan_artifact = json.loads((root / FREEZE_DIR / "plan.json").read_text())
    executable = build_o2c_official_plan_v1()

    assert manifest["domain"] == "o2c"
    assert manifest["status"] == "frozen"
    assert manifest["official_execution_performed"] is False
    assert manifest["scientific_claims_authorized"] is False
    assert manifest["protocol_hash"] == executable.protocol.protocol_hash
    assert manifest["plan_hash"] == executable.plan_hash
    assert protocol_artifact == executable.protocol.canonical_payload()
    assert plan_artifact == executable.canonical_payload()
    assert len(manifest["files"]) == 7
    assert len({*manifest["files"]}) == 7
    for name, expected_hash in manifest["files"].items():
        path = root / name
        assert path.is_file(), name
        assert sha256(path.read_bytes()).hexdigest() == expected_hash, name

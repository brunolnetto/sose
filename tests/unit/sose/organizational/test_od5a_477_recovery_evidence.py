"""#477: fail-closed nonconfirmatory bundle verifier (never scientific admission)."""
from __future__ import annotations

import json

import pytest

from scripts.verify_od5a_477_evidence import ARMS, PROTOCOL, verify


def _bundle(root):
    root.mkdir(parents=True, exist_ok=True)
    for arm in ARMS:
        good = "a" * 64
        record = {
            "protocol": PROTOCOL,
            "evidence_class": "NON_CONFIRMATORY",
            "scientific_admission": "NOT_ADMITTED",
            "arm": arm,
            "git_source_oid": "f" * 40,
            "uv_lock_sha256": "e" * 64,
            "model_spec_hash": "b" * 64,
            "arm_admission_hash": "c" * 64,
            "input_sha256": "d" * 64,
            "step_count": 2,
            "fault_schedule": ["reopen_pg_connection_each_tick"],
            "trace": [
                {
                    "ordinal": i,
                    "action": "advanced" if i == 0 else "complete",
                    "reference_sha256": good,
                    "native_pg_sha256": good,
                    "reopened_pg_sha256": good,
                    "physical_pg_sha256": good,
                    "physical_reopened_sha256": good,
                } for i in range(2)
            ],
        }
        (root / f"{arm}.json").write_text(
            json.dumps(record, sort_keys=True), encoding="utf-8",
        )
    return root


def _mutate(root, action):
    file = root / "baseline.json"
    record = json.loads(file.read_text(encoding="utf-8"))
    action(record)
    file.write_text(json.dumps(record), encoding="utf-8")


def test_exact_five_arm_nonconfirmatory_evidence_passes(tmp_path):
    index = verify(_bundle(tmp_path))
    assert set(index["arms"]) == ARMS
    assert index["scientific_admission"] == "NOT_ADMITTED"
    assert index["signature_status"] == "UNSIGNED_NO_TRUSTED_ISSUER"
    assert not index["unknown_commit_network_certified"]
    assert all(len(x["sha256"]) == 64 for x in index["arms"].values())


@pytest.mark.parametrize("corrupt,reason", [
    (lambda x: x["trace"][0].update(native_pg_sha256="2" * 64), "divergent"),
    (lambda x: x["trace"][1].update(ordinal=0), "out-of-order"),
    (lambda x: x.update(step_count=1), "step count"),
    (lambda x: x["trace"][-1].update(action="advanced"), "incomplete"),
    (lambda x: x.update(scientific_admission="ADMITTED"), "provenance"),
    (lambda x: x.update(uv_lock_sha256="9" * 64), "source / lock"),
    (lambda x: x.update(fault_schedule=["unknown-commit-certified"]), "fault schedule"),
    (lambda x: x.pop("arm_admission_hash"), "arm_admission_hash"),
])
def test_corrupt_bundle_cannot_pass_gate(tmp_path, corrupt, reason):
    _bundle(tmp_path)
    _mutate(tmp_path, corrupt)
    with pytest.raises(ValueError, match=reason):
        verify(tmp_path)


def test_missing_arm_fails_closed(tmp_path):
    _bundle(tmp_path)
    (tmp_path / "baseline.json").unlink()
    with pytest.raises(ValueError, match="incomplete"):
        verify(tmp_path)

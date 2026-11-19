"""Fail-closed verifier for bounded OD-5a #477 PostgreSQL recovery evidence.

Checks recorded five-arm provenance and digest equality, not trusted issuer,
actual OS fault timing, signed certification, or scientific admission.
"""
from __future__ import annotations
import json
import re
from hashlib import sha256
from pathlib import Path
import sys

ARMS = frozenset((
    "baseline", "capacity_only", "topology_only",
    "coordination_only", "structural_combined",
))
HEX40 = re.compile(r"[0-9a-f]{40}\Z")
HEX64 = re.compile(r"[0-9a-f]{64}\Z")
PROTOCOL = "sose-od5a-477-native-pg-recovery-evidence-v1"


def verify(directory: Path) -> dict:
    files = {p.stem: p for p in directory.glob("*.json") if p.name != "index.json"}
    if set(files) != ARMS:
        raise ValueError(f"five-arm recovery bundle incomplete: {sorted(set(files) ^ ARMS)}")
    expected_source = None
    expected_lock = None
    receipts = {}
    for arm, path in sorted(files.items()):
        raw = path.read_bytes()
        evidence = json.loads(raw)
        if (evidence.get("protocol") != PROTOCOL
            or evidence.get("evidence_class") != "NON_CONFIRMATORY"
            or evidence.get("scientific_admission") != "NOT_ADMITTED"
            or evidence.get("arm") != arm):
            raise ValueError(f"invalid experiment provenance: {arm}")
        source = evidence.get("git_source_oid")
        lock = evidence.get("uv_lock_sha256")
        if not isinstance(source, str) or HEX40.fullmatch(source) is None:
            raise ValueError(f"missing exact source commit: {arm}")
        if not isinstance(lock, str) or HEX64.fullmatch(lock) is None:
            raise ValueError(f"missing lockfile digest: {arm}")
        if expected_source is not None and (source != expected_source or lock != expected_lock):
            raise ValueError("five-arm source / lock mismatch")
        expected_source, expected_lock = source, lock
        for key in ("model_spec_hash", "arm_admission_hash", "input_sha256"):
            value = evidence.get(key)
            if not isinstance(value, str) or HEX64.fullmatch(value) is None:
                raise ValueError(f"invalid {key}: {arm}")
        trace = evidence.get("trace")
        if not isinstance(trace, list) or not trace:
            raise ValueError(f"missing full tick trace: {arm}")
        if evidence.get("step_count") != len(trace):
            raise ValueError(f"unreconciled step count: {arm}")
        for i, tick in enumerate(trace):
            if tick.get("ordinal") != i or tick.get("action") not in {"advanced", "complete"}:
                raise ValueError(f"out-of-order trace: {arm} tick {i}")
            if (tick.get("reference_sha256") != tick.get("native_pg_sha256")
                or tick.get("reference_sha256") != tick.get("reopened_pg_sha256")
                or tick.get("physical_pg_sha256") != tick.get("physical_reopened_sha256")):
                raise ValueError(f"divergent full causal/physical trace: {arm} tick {i}")
            for key in (
                "reference_sha256", "native_pg_sha256", "reopened_pg_sha256",
                "physical_pg_sha256", "physical_reopened_sha256",
            ):
                if not isinstance(tick.get(key), str) or HEX64.fullmatch(tick[key]) is None:
                    raise ValueError(f"invalid trace digest: {arm} tick {i} {key}")
        if trace[-1]["action"] != "complete":
            raise ValueError(f"incomplete execution: {arm}")
        if evidence.get("fault_schedule") != ["reopen_pg_connection_each_tick"]:
            raise ValueError(f"unexpected fault schedule: {arm}")
        receipts[arm] = {
            "sha256": sha256(raw).hexdigest(),
            "steps": len(trace),
            "admission_hash": evidence["arm_admission_hash"],
        }
    return {
        "protocol": PROTOCOL,
        "evidence_class": "NON_CONFIRMATORY",
        "scientific_admission": "NOT_ADMITTED",
        "source_git_oid": expected_source,
        "uv_lock_sha256": expected_lock,
        "arms": receipts,
        "signature_status": "UNSIGNED_NO_TRUSTED_ISSUER",
        "physical_server_restart_certified": False,
        "unknown_commit_network_certified": False,
        "fairness_preemption_certified": False,
    }


def main() -> int:
    if len(sys.argv) != 2:
        raise SystemExit("usage: verify_od5a_477_evidence.py <evidence_directory>")
    root = Path(sys.argv[1])
    index = verify(root)
    (root / "index.json").write_text(
        json.dumps(index, indent=2, sort_keys=True) + "\n", encoding="utf-8",
    )
    print(json.dumps(index, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

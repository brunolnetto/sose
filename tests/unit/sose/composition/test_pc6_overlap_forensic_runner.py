"""An intermittent failure's evidence must survive later diagnostic invocations."""
from threading import Lock

from scripts.reproduce_pc6_pg_overlap import (
    capture_first_worker_failure_once,
    create_invocation_directory,
)


def test_distinct_invocations_cannot_overwrite_previous_causal_failure(tmp_path):
    first = create_invocation_directory(tmp_path)
    (first / "run-001.log").write_text("FIRST CAUSAL FAILURE", encoding="utf-8")

    second = create_invocation_directory(tmp_path)
    (second / "run-001.log").write_text("SECOND CAUSAL FAILURE", encoding="utf-8")

    assert first != second
    assert first.parent == second.parent == tmp_path
    assert (first / "run-001.log").read_text(encoding="utf-8") == "FIRST CAUSAL FAILURE"
    assert (second / "run-001.log").read_text(encoding="utf-8") == "SECOND CAUSAL FAILURE"



def test_capture_first_failed_worker_before_later_state_overwrites_evidence():
    captured = []
    snapshots_taken = []
    state = {"committed": "at-first-failure"}

    def read_snapshot():
        snapshots_taken.append(state["committed"])
        return dict(state)

    capture_first_worker_failure_once(
        captured, Lock(), flow="customer-a", error=RuntimeError("broken clock"),
        snapshot=read_snapshot,
    )
    state["committed"] = "later-worker-overwrote-state"
    capture_first_worker_failure_once(
        captured, Lock(), flow="customer-b", error=RuntimeError("late failure"),
        snapshot=read_snapshot,
    )

    assert snapshots_taken == ["at-first-failure"]
    assert len(captured) == 1
    assert captured[0]["flow"] == "customer-a"
    assert "broken clock" in captured[0]["error"]
    assert captured[0]["snapshot"] == {"committed": "at-first-failure"}


def test_first_failure_is_persisted_before_stalled_peer_finishes(tmp_path):
    from pathlib import Path
    from scripts.reproduce_pc6_pg_overlap import capture_first_worker_failure_once

    evidence_path = tmp_path / "run-001.first-failure.json"
    captured = []
    capture_first_worker_failure_once(
        captured, Lock(), flow="customer-a", error=RuntimeError("first failure"),
        snapshot=lambda: {"position": "first-committed-boundary"},
        evidence_path=evidence_path,
    )
    # The peer is still blocked: evidence must already exist and survive
    # process termination, not rely on wait(ALL_COMPLETED) or pytest teardown.
    assert evidence_path.is_file()
    record = __import__("json").loads(evidence_path.read_text(encoding="utf-8"))
    assert record["flow"] == "customer-a"
    assert record["snapshot"]["position"] == "first-committed-boundary"

    capture_first_worker_failure_once(
        captured, Lock(), flow="customer-b", error=RuntimeError("later failure"),
        snapshot=lambda: {"position": "later"}, evidence_path=evidence_path,
    )
    assert __import__("json").loads(evidence_path.read_text(encoding="utf-8")) == record



def test_first_error_is_fsynced_before_potentially_blocking_pg_snapshot(tmp_path):
    import json

    sidecar = tmp_path / "first-exception.json"
    observed = []

    def delayed_snapshot():
        # A future backend lock/query can hang or time out. Its diagnostics
        # must never be a prerequisite for preserving the original failure.
        assert sidecar.exists()
        partial = json.loads(sidecar.read_text(encoding="utf-8"))
        assert partial["flow"] == "customer-a"
        assert "root error" in partial["error"]
        observed.append(partial["snapshot"])
        return {"durable_clock": "before"}

    captured = []
    capture_first_worker_failure_once(
        captured, Lock(), flow="customer-a",
        error=RuntimeError("root error"),
        snapshot=delayed_snapshot, evidence_path=sidecar,
    )
    assert len(observed) == 1
    final = json.loads(sidecar.read_text(encoding="utf-8"))
    assert final["snapshot"] == {"durable_clock": "before"}

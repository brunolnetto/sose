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



def test_sidecar_io_failure_never_masks_primary_worker_error(tmp_path, monkeypatch):
    import os
    original_open = os.open
    target = tmp_path / "unwritable.json"

    def fail_sidecar_open(path, *args, **kwargs):
        if str(path).startswith(str(target) + "."):
            raise OSError("simulated forensic volume failure")
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr(os, "open", fail_sidecar_open)
    root = RuntimeError("ORIGINAL WORKER FAILURE")
    captured = []
    capture_first_worker_failure_once(
        captured, Lock(), flow="customer-b", error=root,
        snapshot=lambda: {"valid_snapshot": True}, evidence_path=target,
    )
    assert captured[0]["error"] == repr(root)
    assert "ORIGINAL WORKER FAILURE" in str(root)
    assert any(
        "forensic" in note.lower()
        for note in getattr(root, "__notes__", ())
    )


def test_atomic_replacement_keeps_first_failure_sidecar_private(tmp_path):
    import os
    import stat

    sidecar = tmp_path / "private-snapshot.json"
    old_umask = os.umask(0o022)
    try:
        capture_first_worker_failure_once(
            [], Lock(), flow="customer-a", error=RuntimeError("private"),
            snapshot=lambda: {"state": "durable"}, evidence_path=sidecar,
        )
    finally:
        os.umask(old_umask)
    assert stat.S_IMODE(sidecar.stat().st_mode) == 0o600


def test_reproducer_logs_and_summary_are_private_under_shared_umask(tmp_path, monkeypatch):
    import os
    import stat
    from types import SimpleNamespace
    from scripts import reproduce_pc6_pg_overlap as forensic

    monkeypatch.setattr(
        forensic.subprocess, "run",
        lambda *args, **kwargs: SimpleNamespace(returncode=1, stdout="failure", stderr="trace"),
    )
    old_umask = os.umask(0o022)
    try:
        run_directory = forensic.create_invocation_directory(tmp_path)
        result = forensic.run_one(1, timeout=3, out=run_directory)
        log_mode = stat.S_IMODE((run_directory / "run-001.log").stat().st_mode)
    finally:
        os.umask(old_umask)
    assert result["status"] == "FAIL"
    assert log_mode == 0o600


def test_forensic_summary_is_private_even_with_umask_022(tmp_path, monkeypatch):
    import json
    import os
    import stat
    from scripts import reproduce_pc6_pg_overlap as forensic

    monkeypatch.setenv("SOSE_TEST_POSTGRES_DSN", "postgresql://dummy")
    monkeypatch.setattr(
        forensic, "run_one",
        lambda index, *, timeout, out: {
            "run": index, "status": "PASS", "exit_code": 0,
            "log": str(out / "private.log"), "first_failure_evidence": None,
        },
    )
    monkeypatch.setattr(
        "sys.argv",
        ["reproducer", "--runs", "1", "--parallel", "1", "--out", str(tmp_path)],
    )
    old_umask = os.umask(0o022)
    try:
        assert forensic.main() == 0
        summaries = list(tmp_path.glob("*/summary.json"))
        assert len(summaries) == 1
        assert stat.S_IMODE(summaries[0].stat().st_mode) == 0o600
        assert json.loads(summaries[0].read_text())["runs"] == 1
    finally:
        os.umask(old_umask)


def test_failed_initial_sidecar_fsync_never_exposes_truncated_evidence(tmp_path, monkeypatch):
    import os
    from scripts.reproduce_pc6_pg_overlap import capture_first_worker_failure_once

    target = tmp_path / "first-failure.json"
    original_fsync = os.fsync

    def fail_fsync(fd):
        raise OSError("simulated disk full during first evidence fsync")

    monkeypatch.setattr(os, "fsync", fail_fsync)
    root = RuntimeError("primary worker failure")
    capture_first_worker_failure_once(
        [], Lock(), flow="customer-a", error=root,
        snapshot=lambda: {"durable": True}, evidence_path=target,
    )
    monkeypatch.setattr(os, "fsync", original_fsync)
    assert not target.exists()
    assert "primary worker failure" in str(root)
    assert any("forensic initial sidecar write failed" in n for n in root.__notes__)

"""Forensic first-failure evidence must survive peer hangs and I/O failures."""

import json
import os
from threading import Lock

from scripts.reproduce_pc6_pg_overlap import capture_first_worker_failure_once


def test_first_worker_failure_is_published_as_complete_private_sidecar(tmp_path):
    evidence_path = tmp_path / "first-failure.json"
    captured = []
    original = RuntimeError("worker A failed")
    capture_first_worker_failure_once(
        captured,
        Lock(),
        flow="customer-a",
        error=original,
        snapshot=lambda: {"record_identities": [["events", "0", 0]]},
        evidence_path=evidence_path,
    )
    payload = json.loads(evidence_path.read_text(encoding="utf-8"))
    assert payload["flow"] == "customer-a"
    assert "worker A failed" in payload["error"]
    assert payload["snapshot"]["record_identities"] == [["events", "0", 0]]
    assert captured[0]["snapshot"] == payload["snapshot"]
    assert os.stat(evidence_path).st_mode & 0o777 == 0o600
    assert not tuple(tmp_path.glob("*.tmp"))


def test_initial_fsync_failure_never_advertises_truncated_evidence(tmp_path, monkeypatch):
    evidence_path = tmp_path / "first-failure.json"
    original = RuntimeError("original worker failure")
    captured = []

    def fail_fsync(_fd):
        raise OSError("forensic volume full")

    monkeypatch.setattr(os, "fsync", fail_fsync)
    capture_first_worker_failure_once(
        captured,
        Lock(),
        flow="customer-b",
        error=original,
        snapshot=lambda: {"status": "completed"},
        evidence_path=evidence_path,
    )

    assert not evidence_path.exists()
    assert not tuple(tmp_path.glob("*.tmp"))
    assert captured[0]["snapshot"] == {"status": "completed"}
    assert any("forensic initial sidecar write failed" in note for note in original.__notes__)


def test_failed_snapshot_replacement_preserves_first_complete_exception(tmp_path, monkeypatch):
    evidence_path = tmp_path / "first-failure.json"
    original = RuntimeError("original worker failure")
    captured = []

    def fail_replace(*_args, **_kwargs):
        raise OSError("replacement unavailable")

    monkeypatch.setattr(os, "replace", fail_replace)
    capture_first_worker_failure_once(
        captured,
        Lock(),
        flow="customer-c",
        error=original,
        snapshot=lambda: {"records": 42},
        evidence_path=evidence_path,
    )

    persisted = json.loads(evidence_path.read_text(encoding="utf-8"))
    assert persisted["flow"] == "customer-c"
    assert "original worker failure" in persisted["error"]
    assert persisted["snapshot"]["status"] == "pending-read-only-snapshot"
    assert captured[0]["snapshot"] == {"records": 42}
    assert any("sidecar replacement failed" in note for note in original.__notes__)
    assert not tuple(tmp_path.glob("*.tmp"))

"""Bounded opt-in stress reproducer for PC6 PostgreSQL race (#468).

It re-runs the *real two-worker statechart* test in independent subprocesses.
Every test invocation creates a unique PostgreSQL namespace. Findings are
retained as forensic logs, not dismissed as flaky or auto-retried to green.

Usage:
  SOSE_TEST_POSTGRES_DSN='postgresql://...' \
    python scripts/reproduce_pc6_pg_overlap.py --runs 12 --parallel 3 --out /tmp/pc6-forensics

Requires pytest and the repository's editable development dependencies.
"""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from collections.abc import Callable
import traceback
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import subprocess
import sys
from uuid import uuid4


TARGET = (
    "tests/e2e/sose/persistence/test_postgres_pc6_concurrent_real_statecharts.py"
    "::test_pg_two_real_customer_statecharts_overlap_without_business_aliasing"
)



def capture_first_worker_failure_once(
    captured: list[dict[str, object]],
    capture_lock,
    *,
    flow: str,
    error: Exception,
    snapshot: Callable[[], dict[str, object]],
    evidence_path: Path | None = None,
) -> None:
    """Record committed state on first worker exception, not after wait(ALL_COMPLETED).

    Locking is restricted to failure evidence collection. Healthy workers and
    distinct organizational persistence transactions are never serialized here.
    """
    with capture_lock:
        if captured:
            return
        record = {
            "flow": flow,
            "error": repr(error),
            "traceback": "".join(traceback.format_exception(error))[-4000:],
            "snapshot": {"status": "pending-read-only-snapshot"},
        }
        captured.append(record)
        sidecar_written = False
        if evidence_path is not None:
            # Publish only a complete, fsynced record; do not leave truncated
            # sidecars that the stress harness could mistake for evidence.
            initial_tmp = evidence_path.with_name(
                evidence_path.name + "." + uuid4().hex + ".tmp"
            )
            try:
                fd = os.open(initial_tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                with os.fdopen(fd, "w", encoding="utf-8") as handle:
                    json.dump(record, handle, sort_keys=True, default=str)
                    handle.write("\n")
                    handle.flush()
                    os.fsync(handle.fileno())
                os.link(initial_tmp, evidence_path)  # Exclusive publish, never overwrite.
                sidecar_written = True
            except OSError as io_error:
                error.add_note(f"forensic initial sidecar write failed: {io_error!r}")
            finally:
                try:
                    initial_tmp.unlink(missing_ok=True)
                except OSError:
                    pass
        try:
            record["snapshot"] = snapshot()
        except Exception as snapshot_error:
            record["snapshot"] = {"snapshot_error": repr(snapshot_error)}
        if evidence_path is not None and sidecar_written:
            # A failed replacement leaves the FIRST durable exception intact.
            # Never exchange its private inode for an umask-permissive temp.
            temporary = evidence_path.with_name(
                evidence_path.name + "." + uuid4().hex + ".tmp"
            )
            try:
                fd = os.open(
                    temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600
                )
                with os.fdopen(fd, "w", encoding="utf-8") as handle:
                    json.dump(record, handle, sort_keys=True, default=str)
                    handle.write("\n")
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(temporary, evidence_path)
            except OSError as io_error:
                error.add_note(
                    f"forensic snapshot sidecar replacement failed: {io_error!r}"
                )
                try:
                    temporary.unlink(missing_ok=True)
                except OSError:
                    pass


def create_invocation_directory(parent: Path) -> Path:
    """Never overwrite earlier forensic evidence, even under concurrent invocations."""
    parent.mkdir(parents=True, exist_ok=True)
    directory = parent / (
        datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
        + "-" + uuid4().hex[:10]
    )
    directory.mkdir(parents=False, exist_ok=False)
    return directory


def run_one(run_index: int, *, timeout: int, out: Path) -> dict[str, object]:
    command = [sys.executable, "-m", "pytest", "-q", TARGET, "--tb=long", "-x"]
    # A hung peer may prevent pytest from flushing its captured exception.
    # Give each subprocess a unique durable sidecar for its *first* failure.
    evidence_path = out / f"run-{run_index:03d}.first-failure.json"
    env = dict(os.environ)
    env["SOSE_PC6_FIRST_FAILURE_SNAPSHOT"] = str(evidence_path)
    try:
        process = subprocess.run(
            command, capture_output=True, text=True, timeout=timeout,
            check=False, env=env,
        )
        exit_code, output = process.returncode, process.stdout + "\n" + process.stderr
        status = "PASS" if exit_code == 0 else "FAIL"
    except subprocess.TimeoutExpired as error:
        exit_code, status = 124, "TIMEOUT"
        stdout = error.stdout or b""
        stderr = error.stderr or b""
        output = (
            stdout.decode(errors="replace") if isinstance(stdout, bytes) else stdout
        ) + "\n" + (
            stderr.decode(errors="replace") if isinstance(stderr, bytes) else stderr
        )
    logfile = out / f"run-{run_index:03d}.log"
    fd = os.open(logfile, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(output)
        handle.flush()
        os.fsync(handle.fileno())
    return {
        "run": run_index,
        "status": status,
        "exit_code": exit_code,
        "log": str(logfile),
        "first_failure_evidence": str(evidence_path) if evidence_path.exists() else None,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runs", type=int, default=12)
    parser.add_argument("--parallel", type=int, default=3)
    parser.add_argument("--timeout", type=int, default=120)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    if args.runs < 1 or args.parallel < 1 or args.timeout < 1:
        parser.error("runs, parallel and timeout must be positive")
    if not os.environ.get("SOSE_TEST_POSTGRES_DSN"):
        parser.error("SOSE_TEST_POSTGRES_DSN is required; skipped tests are not evidence")

    run_directory = create_invocation_directory(args.out)
    started = datetime.now(timezone.utc).isoformat()
    with ThreadPoolExecutor(max_workers=min(args.parallel, args.runs)) as executor:
        futures = {
            executor.submit(run_one, i, timeout=args.timeout, out=run_directory): i
            for i in range(1, args.runs + 1)
        }
        results = [f.result() for f in as_completed(futures)]

    results.sort(key=lambda record: record["run"])
    summary = {
        "issue": 468,
        "started_at": started,
        "test": TARGET,
        "run_directory": str(run_directory),
        "runs": args.runs,
        "parallel": min(args.parallel, args.runs),
        "timeout_seconds": args.timeout,
        "passed": sum(r["status"] == "PASS" for r in results),
        "failed": sum(r["status"] == "FAIL" for r in results),
        "timed_out": sum(r["status"] == "TIMEOUT" for r in results),
        "results": results,
        "note": "No failure observed is NOT a proof of concurrency correctness.",
    }
    summary_path = run_directory / "summary.json"
    fd = os.open(summary_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(json.dumps(summary, sort_keys=True, indent=2) + "\n")
        handle.flush()
        os.fsync(handle.fileno())
    print(json.dumps(summary, sort_keys=True, indent=2))
    return 0 if summary["failed"] == 0 and summary["timed_out"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())

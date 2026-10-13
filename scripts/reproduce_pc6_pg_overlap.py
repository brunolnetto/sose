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
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import subprocess
import sys


TARGET = (
    "tests/e2e/sose/persistence/test_postgres_pc6_concurrent_real_statecharts.py"
    "::test_pg_two_real_customer_statecharts_overlap_without_business_aliasing"
)


def run_one(run_index: int, *, timeout: int, out: Path) -> dict[str, object]:
    command = [sys.executable, "-m", "pytest", "-q", TARGET, "--tb=long", "-x"]
    try:
        process = subprocess.run(
            command, capture_output=True, text=True, timeout=timeout, check=False,
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
    logfile.write_text(output, encoding="utf-8")
    return {
        "run": run_index,
        "status": status,
        "exit_code": exit_code,
        "log": str(logfile),
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

    args.out.mkdir(parents=True, exist_ok=True)
    started = datetime.now(timezone.utc).isoformat()
    with ThreadPoolExecutor(max_workers=min(args.parallel, args.runs)) as executor:
        futures = {
            executor.submit(run_one, i, timeout=args.timeout, out=args.out): i
            for i in range(1, args.runs + 1)
        }
        results = [f.result() for f in as_completed(futures)]

    results.sort(key=lambda record: record["run"])
    summary = {
        "issue": 468,
        "started_at": started,
        "test": TARGET,
        "runs": args.runs,
        "parallel": min(args.parallel, args.runs),
        "timeout_seconds": args.timeout,
        "passed": sum(r["status"] == "PASS" for r in results),
        "failed": sum(r["status"] == "FAIL" for r in results),
        "timed_out": sum(r["status"] == "TIMEOUT" for r in results),
        "results": results,
        "note": "No failure observed is NOT a proof of concurrency correctness.",
    }
    (args.out / "summary.json").write_text(
        json.dumps(summary, sort_keys=True, indent=2) + "\n", encoding="utf-8",
    )
    print(json.dumps(summary, sort_keys=True, indent=2))
    return 0 if summary["failed"] == 0 and summary["timed_out"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())

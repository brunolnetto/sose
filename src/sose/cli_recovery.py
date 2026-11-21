"""``sose composition-recovery``: parser, run preflight and the scoped re-binding (#637)."""

from __future__ import annotations

import argparse
import sys
from dataclasses import asdict
from datetime import datetime, timezone
from typing import Any, Callable

from sose.cli_validation import _json, _recovery_store

_RUN_ONLY = (
    ("resource_policies", "--resource-policies"),
    ("start_at", "--start-at"),
    ("interval_seconds", "--interval-seconds"),
)


def add_composition_recovery_parser(
    subparsers: Any, handler: Callable[[argparse.Namespace], int],
) -> None:
    recovery = subparsers.add_parser(
        "composition-recovery",
        help="Run deterministic PC6 catch-up once (cron) or as a recurring worker.",
    )
    recovery.add_argument("--postgres-dsn", required=True, help="Authoritative PostgreSQL DSN.")
    recovery.add_argument("--namespace", default="sose", help="PostgreSQL namespace.")
    recovery.add_argument(
        "--resource-policies",
        help="JSON file mapping intent names to resource pool contracts (required to run).",
    )
    recovery.add_argument("--owner-id", required=True, help="Worker lease identity, not the organization.")
    recovery.add_argument("--correlation-id", required=True, help="Durable flow correlation handled by this worker.")
    recovery.add_argument("--organization-id", required=True, help="Authoritative organizational owner of the flow runtime.")
    recovery.add_argument("--start-at", help="ISO 8601 timezone-aware anchor (required to run).")
    recovery.add_argument("--interval-seconds", type=int, help="Schedule interval (required to run).")
    recovery.add_argument("--max-slots", type=int, default=16)
    recovery.add_argument("--max-actions", type=int, default=16)
    recovery.add_argument("--now", help="ISO 8601 test/replay time (one-shot only).")
    recovery.add_argument("--serve", action="store_true", help="Poll continuously.")
    recovery.add_argument("--poll-seconds", type=float, default=5.0)
    recovery.add_argument(
        "--scoped-writer", action="store_true",
        help="Run under the job-scoped writer lease (a re-bound or new scoped job). "
             "Running without it (the namespace-wide lease) is deprecated since 0.13.0 "
             "and removed in 0.14.0.",
    )
    recovery.add_argument(
        "--rebind-scoped", action="store_true",
        help="Do not run: fence the global lease and re-bind the existing job to "
             "its job-scoped lease, keeping its job_id and trigger history.",
    )
    recovery.set_defaults(handler=handler)


def recovery_run_start(args: argparse.Namespace) -> datetime:
    """Validate a schedule run's options before any store is opened; return its anchor."""
    missing = [flag for name, flag in _RUN_ONLY if getattr(args, name) is None]
    if missing:
        raise ValueError("composition recovery requires " + ", ".join(missing))
    if args.serve and args.now is not None:
        raise ValueError("--now is only supported for one-shot execution")
    if args.serve and args.poll_seconds <= 0:
        raise ValueError("--poll-seconds must be positive")
    start_at = datetime.fromisoformat(args.start_at)
    if start_at.tzinfo is None:
        raise ValueError("start_at must be timezone-aware")
    return start_at


def warn_global_lease(args: argparse.Namespace) -> None:
    """Tell CLI operators on stderr: Python hides a DeprecationWarning raised in a library."""
    if args.scoped_writer:
        return
    from sose._deprecation import deprecation_message
    from sose.composition.recovery import GLOBAL_LEASE_REMOVED_IN, GLOBAL_LEASE_SINCE

    print("warning: " + deprecation_message(
        "sose composition-recovery without --scoped-writer (the namespace-wide writer lease)",
        since=GLOBAL_LEASE_SINCE, removed_in=GLOBAL_LEASE_REMOVED_IN,
        instead="run --rebind-scoped once for an existing job, then run with --scoped-writer",
    ), file=sys.stderr, flush=True)


def cmd_composition_rebind(args: argparse.Namespace) -> int:
    """Re-bind the global-lease recovery job to its scoped lease; print the JSON report."""
    from sose.composition.recovery_rebind import rebind_recovery_job_to_scope

    if args.serve:
        raise ValueError("--rebind-scoped is a one-shot operation; --serve is not allowed")
    now = datetime.fromisoformat(args.now) if args.now else datetime.now(timezone.utc)
    store = _recovery_store(args)
    try:
        report = rebind_recovery_job_to_scope(
            store,
            job_id="trading-company-customer-recovery",
            owner_id=args.owner_id,
            correlation_id=args.correlation_id,
            organization_id=args.organization_id,
            now=now,
        )
    finally:
        store.close()
    print(_json(asdict(report)), flush=True)
    return 0

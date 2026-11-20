from __future__ import annotations

import argparse
import sys

# Names that lived here before the split stay importable from ``sose.cli``.

from sose.cli_recovery import add_composition_recovery_parser
from sose.cli_commands import (
    _cmd_apply,
    _cmd_composition_recovery,
    _cmd_config_set,
    _cmd_config_show,
    _cmd_doctor,
    _cmd_domains,
    _cmd_init,
    _cmd_inspect,
    _cmd_job_set_policy,
    _cmd_job_show,
    _cmd_migrate_owners,
    _cmd_persistence,
    _cmd_plan,
    _cmd_run,
    _cmd_sinks,
    _cmd_storage,
    _cmd_storage_add_sink,
    _cmd_storage_remove_sink,
    _cmd_storage_set_authoritative,
    _cmd_trigger,
    _cmd_validate,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="sose",
        description="Synthetic Operational System Engine",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    add_composition_recovery_parser(subparsers, _cmd_composition_recovery)

    migrate = subparsers.add_parser(
        "migrate-owners",
        help="Offline: migrate an unowned SOSE <= 0.8 SQLite database to an owned one.",
    )
    migrate.add_argument("--sqlite", required=True, help="Source SOSE <= 0.8 database.")
    migrate.add_argument("--output", required=True, help="New database to write.")
    migrate.add_argument("--owner", required=True, help="Owner of the database's runtime.")
    migrate.add_argument("--report", help="Write the JSON migration report here.")
    migrate.set_defaults(handler=_cmd_migrate_owners)

    init = subparsers.add_parser(
        "init",
        help="Create a sose.toml file from a domain's validated defaults.",
    )
    init.add_argument("--domain", required=True)
    init.add_argument("--output", default="sose.toml")
    init.add_argument("--job-id")
    init.add_argument(
        "--persistence",
        default="sqlite_incremental",
        help="Persistence adapter name.",
    )
    init.add_argument(
        "--persistence-path",
        default="state/sose.sqlite3",
    )
    init.add_argument("--force", action="store_true")
    init.set_defaults(handler=_cmd_init)

    validate = subparsers.add_parser(
        "validate",
        help="Validate a sose.toml file and domain parameters.",
    )
    validate.add_argument("--config", default="sose.toml")
    validate.set_defaults(handler=_cmd_validate)

    config_cmd = subparsers.add_parser(
        "config",
        help="Inspect or edit validated domain parameters in sose.toml.",
    )
    config_subparsers = config_cmd.add_subparsers(
        dest="config_command",
        required=True,
    )

    config_show = config_subparsers.add_parser(
        "show",
        help="Show effective domain parameters and mutability.",
    )
    config_show.add_argument("--config", default="sose.toml")
    config_show.set_defaults(handler=_cmd_config_show)

    config_set = config_subparsers.add_parser(
        "set",
        help="Validate and edit one [domain.parameters] value.",
    )
    config_set.add_argument("name")
    config_set.add_argument("value")
    config_set.add_argument("--config", default="sose.toml")
    config_set.set_defaults(handler=_cmd_config_set)

    apply = subparsers.add_parser(
        "apply",
        help="Explicitly apply sose.toml domain parameters to a durable job.",
    )
    apply.add_argument("--config", default="sose.toml")
    apply.set_defaults(handler=_cmd_apply)

    run = subparsers.add_parser(
        "run",
        help="Advance exactly one durable simulation tick.",
    )
    run.add_argument("--config", default="sose.toml")
    run.add_argument("--trigger-id")
    run.add_argument(
        "--recover",
        action="store_true",
        help="Recover the same unresolved trigger explicitly.",
    )
    run.set_defaults(handler=_cmd_run)

    trigger = subparsers.add_parser(
        "trigger",
        help="Execute one durable recurring trigger using the configured tick batch.",
    )
    trigger.add_argument("--config", default="sose.toml")
    trigger_identity = trigger.add_mutually_exclusive_group(required=True)
    trigger_identity.add_argument(
        "--trigger-id",
        help="Stable id from the external scheduler/run attempt.",
    )
    trigger_identity.add_argument(
        "--scheduled-for",
        help=(
            "Timezone-aware scheduled occurrence (ISO-8601). "
            "SOSE derives a stable trigger id from job id + UTC instant."
        ),
    )
    trigger.add_argument(
        "--ticks",
        type=int,
        help="Override ticks_per_trigger for this trigger.",
    )
    trigger.add_argument(
        "--recover",
        action="store_true",
        help="Resume this same partially completed trigger explicitly.",
    )
    trigger.set_defaults(handler=_cmd_trigger)

    inspect = subparsers.add_parser(
        "inspect",
        help="Inspect durable job checkpoint without advancing it.",
    )
    inspect.add_argument("--config", default="sose.toml")
    inspect.set_defaults(handler=_cmd_inspect)

    doctor = subparsers.add_parser(
        "doctor",
        help="Validate config and inspect durable job/runtime consistency.",
    )
    doctor.add_argument("--config", default="sose.toml")
    doctor.set_defaults(handler=_cmd_doctor)

    domains = subparsers.add_parser(
        "domains",
        help="List builtin domains or inspect one domain's parameters.",
    )
    domains.add_argument(
        "--name",
        help="Show defaults, types, and constraints for one domain.",
    )
    domains.set_defaults(handler=_cmd_domains)

    plan = subparsers.add_parser(
        "plan",
        help="Resolve the complete declarative execution plan without side effects.",
    )
    plan.add_argument("--config", default="sose.toml")
    plan.set_defaults(handler=_cmd_plan)

    job = subparsers.add_parser(
        "job",
        help="Inspect or edit durable recurring job policy.",
    )
    job_subparsers = job.add_subparsers(
        dest="job_command",
        required=True,
    )

    job_show = job_subparsers.add_parser(
        "show",
        help="Show logical tick policy for the recurring job.",
    )
    job_show.add_argument("--config", default="sose.toml")
    job_show.set_defaults(handler=_cmd_job_show)

    job_policy = job_subparsers.add_parser(
        "set-policy",
        help="Edit ticks-per-trigger bounds in sose.toml.",
    )
    job_policy.add_argument("--config", default="sose.toml")
    job_policy.add_argument("--ticks-per-trigger", type=int)
    job_policy.add_argument("--max-ticks-per-trigger", type=int)
    job_policy.set_defaults(handler=_cmd_job_set_policy)

    storage = subparsers.add_parser(
        "storage",
        help="Inspect or edit authoritative persistence and analytical sinks.",
    )
    storage.add_argument("--config", default="sose.toml")
    storage.set_defaults(handler=_cmd_storage)
    storage_subparsers = storage.add_subparsers(
        dest="storage_command",
        required=False,
    )

    storage_authoritative = storage_subparsers.add_parser(
        "set-authoritative",
        help="Set the authoritative persistence adapter in sose.toml.",
    )
    storage_authoritative.add_argument("adapter")
    storage_authoritative.add_argument(
        "--option",
        action="append",
        default=[],
        metavar="KEY=VALUE",
        help="Adapter option. Repeat for multiple values.",
    )
    storage_authoritative.add_argument(
        "--require",
        action="append",
        default=[],
        metavar="CAPABILITY",
        help="Required persistence capability. Repeat as needed.",
    )
    storage_authoritative.add_argument("--config", default="sose.toml")
    storage_authoritative.set_defaults(handler=_cmd_storage_set_authoritative)

    storage_add_sink = storage_subparsers.add_parser(
        "add-sink",
        help="Add one analytical sink to sose.toml.",
    )
    storage_add_sink.add_argument("name")
    storage_add_sink.add_argument("adapter")
    storage_add_sink.add_argument(
        "--option",
        action="append",
        default=[],
        metavar="KEY=VALUE",
        help="Sink option. Repeat for multiple values.",
    )
    storage_add_sink.add_argument("--config", default="sose.toml")
    storage_add_sink.set_defaults(handler=_cmd_storage_add_sink)

    storage_remove_sink = storage_subparsers.add_parser(
        "remove-sink",
        help="Remove one analytical sink from sose.toml.",
    )
    storage_remove_sink.add_argument("name")
    storage_remove_sink.add_argument("--config", default="sose.toml")
    storage_remove_sink.set_defaults(handler=_cmd_storage_remove_sink)

    persistence = subparsers.add_parser(
        "persistence",
        help="List builtin persistence adapters.",
    )
    persistence.set_defaults(handler=_cmd_persistence)

    sinks = subparsers.add_parser(
        "sinks",
        help="List builtin analytical sink adapters.",
    )
    sinks.set_defaults(handler=_cmd_sinks)

    return parser


def run_cli(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.handler(args))
    except Exception as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


def main() -> None:
    raise SystemExit(run_cli())


if __name__ == "__main__":
    main()

from __future__ import annotations

import argparse
from dataclasses import asdict
import json
from pathlib import Path
import sys
from datetime import datetime

from sose.jobs.config import load_sose_config
from sose.jobs.config_edit import (
    describe_domain_config,
    parse_cli_value,
    set_domain_parameter,
)
from sose.jobs.doctor import inspect_job_file_health
from sose.jobs.factory import _domain_warehouse, build_job_from_file
from sose.jobs.job_edit import describe_job_policy, set_job_policy
from sose.jobs.plan import build_execution_plan_from_file
from sose.jobs.storage import build_storage_plan
from sose.jobs.storage_edit import (
    add_analytical_sink,
    parse_key_value_options,
    remove_analytical_sink,
    set_authoritative_storage,
)
from sose.persistence.registry import builtin_persistence_registry
from sose.sinks.registry import builtin_sink_registry


def _json(value: object) -> str:
    return json.dumps(value, indent=2, sort_keys=True, default=str)


def _close_persistence(persistence) -> None:
    close = getattr(persistence, "close", None)
    if callable(close):
        close()


def _validate_sqlite_domain_warehouse_options(options: dict[str, object]) -> None:
    path_value = options.pop("path", "state/domain.sqlite3")
    if not isinstance(path_value, str) or not path_value:
        raise ValueError(
            "sqlite DomainWarehouse path must be a non-empty string"
        )


def _validate_postgres_domain_warehouse_options(options: dict[str, object]) -> None:
    for name in ("dsn", "dsn_env"):
        value = options.pop(name, None)
        if value is not None and (not isinstance(value, str) or not value):
            raise ValueError(
                f"postgres DomainWarehouse {name} must be a non-empty string"
            )
    namespace = options.pop("namespace", "sose_domain")
    if not isinstance(namespace, str) or not namespace:
        raise ValueError(
            "postgres DomainWarehouse namespace must be a non-empty string"
        )


def _validate_domain_warehouse_section(section) -> None:
    options = dict(section.options)
    if section.adapter == "sqlite":
        _validate_sqlite_domain_warehouse_options(options)
    elif section.adapter == "postgres":
        _validate_postgres_domain_warehouse_options(options)
    else:
        raise KeyError(f"unknown DomainWarehouse adapter: {section.adapter}")
    if options:
        raise ValueError(
            f"unknown {section.adapter} DomainWarehouse options: {sorted(options)}"
        )


def _validate_config(path: str | Path) -> dict[str, object]:
    from sose.examples.catalog import builtin_catalog

    config, _ = load_sose_config(path)
    domains = builtin_catalog()
    definition = domains.get(config.domain.name)
    resolved = definition.parse_config(config.domain.parameters)

    persistence_registry = builtin_persistence_registry()
    sink_registry = builtin_sink_registry()
    storage_plan = build_storage_plan(
        config,
        persistence_registry=persistence_registry,
        sink_registry=sink_registry,
    )

    if config.runtime.backend != "simpy":
        raise KeyError(f"unknown runtime backend: {config.runtime.backend}")

    # Validate the public DomainWarehouse section without opening a database.
    if config.domain_warehouse is not None:
        _validate_domain_warehouse_section(config.domain_warehouse)

    return {
        "job_id": config.job.id,
        "domain": definition.name,
        "domain_parameters": resolved.model_dump(mode="json"),
        "storage": storage_plan.describe(),
        "persistence": config.persistence.adapter,
        "persistence_require": list(config.persistence.require),
        "runtime_backend": config.runtime.backend,
        "sinks": [
            {"name": sink.name, "adapter": sink.adapter}
            for sink in config.sinks
        ],
    }


def _cmd_init(args: argparse.Namespace) -> int:
    from sose.examples.catalog import builtin_catalog
    from sose.jobs.scaffold import write_sose_toml

    definition = builtin_catalog().get(args.domain)
    output = write_sose_toml(
        args.output,
        definition,
        job_id=args.job_id,
        persistence_adapter=args.persistence,
        persistence_path=args.persistence_path,
        force=args.force,
    )
    description = definition.describe_config()
    print(str(output))
    print(
        f'Initialized {len(description["defaults"])} editable domain defaults '
        "in [domain.parameters]."
    )
    print("Edit them in sose.toml, then run: sose validate && sose apply")
    return 0


def _cmd_validate(args: argparse.Namespace) -> int:
    validated = _validate_config(args.config)
    print(_json(validated))
    return 0


def _cmd_config_show(args: argparse.Namespace) -> int:
    print(_json(describe_domain_config(args.config)))
    return 0


def _cmd_config_set(args: argparse.Namespace) -> int:
    result = set_domain_parameter(
        args.config,
        name=args.name,
        value=parse_cli_value(args.value),
    )
    print(_json(result))
    return 0


def _cmd_apply(args: argparse.Namespace) -> int:
    config, _ = load_sose_config(args.config)
    job = build_job_from_file(args.config)
    try:
        before = job.state()
        applied = job.apply_config(config.domain.parameters)
        print(
            _json(
                {
                    "job_id": applied.job_id,
                    "domain": applied.domain_name,
                    "config_revision": applied.config_revision,
                    "changed": (
                        before is None
                        or applied.config_revision != before.config_revision
                    ),
                    "config": json.loads(applied.config_json),
                }
            )
        )
        return 0
    finally:
        job.close()


def _cmd_run(args: argparse.Namespace) -> int:
    job = build_job_from_file(args.config)
    try:
        result = job.run_tick(
            trigger_id=args.trigger_id,
            recover=args.recover,
        )
        print(_json(asdict(result)))
        return 0
    finally:
        job.close()


def _cmd_trigger(args: argparse.Namespace) -> int:
    job = build_job_from_file(args.config)
    try:
        if args.scheduled_for is not None:
            scheduled_for = datetime.fromisoformat(
                args.scheduled_for.replace("Z", "+00:00")
            )
            result = job.run_scheduled_trigger(
                scheduled_for=scheduled_for,
                ticks=args.ticks,
                recover=args.recover,
            )
        else:
            result = job.run_trigger(
                trigger_id=args.trigger_id,
                ticks=args.ticks,
                recover=args.recover,
            )
        print(_json(asdict(result)))
        return 0
    finally:
        job.close()


def _cmd_inspect(args: argparse.Namespace) -> int:
    config, base_dir = load_sose_config(args.config)
    registry = builtin_persistence_registry()
    persistence = registry.create(
        config.persistence.adapter,
        config.persistence.options,
        base_dir=base_dir,
    )
    try:
        state = persistence.job_state(config.job.id)
        position = persistence.simulation_position()
        diagnostics = None
        if state is not None:
            diagnostics = {
                "job": asdict(state),
                "position": (
                    None if position is None else asdict(position)
                ),
                "config": json.loads(state.config_json),
            }
        else:
            diagnostics = {
                "job": None,
                "position": (
                    None if position is None else asdict(position)
                ),
            }
        print(_json(diagnostics))
        return 0
    finally:
        _close_persistence(persistence)


def _cmd_doctor(args: argparse.Namespace) -> int:
    report = inspect_job_file_health(args.config)
    print(_json(report.to_dict()))
    return 0 if report.healthy else 1


def _cmd_domains(args: argparse.Namespace) -> int:
    from sose.examples.catalog import builtin_catalog

    catalog = builtin_catalog()
    if args.name:
        print(_json(catalog.get(args.name).describe_config()))
        return 0

    payload = [
        {
            "name": definition.name,
            "description": definition.description,
            "config_model": definition.config_model.__name__,
        }
        for definition in catalog.definitions()
    ]
    print(_json(payload))
    return 0





def _cmd_plan(args: argparse.Namespace) -> int:
    print(_json(build_execution_plan_from_file(args.config)))
    return 0


def _cmd_job_show(args: argparse.Namespace) -> int:
    print(_json(describe_job_policy(args.config)))
    return 0


def _cmd_job_set_policy(args: argparse.Namespace) -> int:
    result = set_job_policy(
        args.config,
        ticks_per_trigger=args.ticks_per_trigger,
        max_ticks_per_trigger=args.max_ticks_per_trigger,
    )
    print(_json(result))
    return 0


def _cmd_storage(args: argparse.Namespace) -> int:
    config, _ = load_sose_config(args.config)
    plan = build_storage_plan(
        config,
        persistence_registry=builtin_persistence_registry(),
        sink_registry=builtin_sink_registry(),
    )
    print(_json(plan.describe()))
    return 0 if plan.healthy else 1



def _cmd_storage_set_authoritative(args: argparse.Namespace) -> int:
    result = set_authoritative_storage(
        args.config,
        adapter=args.adapter,
        options=parse_key_value_options(args.option),
        require=tuple(args.require),
    )
    print(_json(result))
    return 0


def _cmd_storage_add_sink(args: argparse.Namespace) -> int:
    result = add_analytical_sink(
        args.config,
        name=args.name,
        adapter=args.adapter,
        options=parse_key_value_options(args.option),
    )
    print(_json(result))
    return 0


def _cmd_storage_remove_sink(args: argparse.Namespace) -> int:
    result = remove_analytical_sink(
        args.config,
        name=args.name,
    )
    print(_json(result))
    return 0


def _cmd_persistence(args: argparse.Namespace) -> int:
    registry = builtin_persistence_registry()
    print(
        _json(
            {
                "adapters": [
                    {
                        "name": adapter.name,
                        "optional_extra": adapter.optional_extra,
                        "capabilities": list(adapter.capabilities.names()),
                    }
                    for adapter in registry.describe()
                ]
            }
        )
    )
    return 0


def _cmd_sinks(args: argparse.Namespace) -> int:
    registry = builtin_sink_registry()
    print(
        _json(
            {
                "adapters": [
                    {
                        "name": adapter.name,
                        "optional_extra": adapter.optional_extra,
                    }
                    for adapter in (
                        registry.adapter(name)
                        for name in registry.names()
                    )
                ]
            }
        )
    )
    return 0


def _cmd_composition_recovery(args: argparse.Namespace) -> int:
    """Invoke durable catch-up once (cron) or run as a polling worker."""
    from datetime import timedelta, timezone
    from time import sleep

    from sose.composition.recovery import TradingCustomerRecoveryRunner
    from sose.composition.scheduler import RecoverySchedule
    from sose.persistence.sqlite_incremental import SQLiteIncrementalPersistence

    if args.serve and args.now is not None:
        raise ValueError("--now is only supported for one-shot execution")
    if args.serve and args.poll_seconds <= 0:
        raise ValueError("--poll-seconds must be positive")
    store = SQLiteIncrementalPersistence(args.sqlite)
    try:
        runner = TradingCustomerRecoveryRunner(
            persistence=store,
            owner_id=args.owner_id,
            max_actions=args.max_actions,
        )
        scheduler = RecoverySchedule(
            runner=runner,
            start_at=datetime.fromisoformat(args.start_at),
            interval=timedelta(seconds=args.interval_seconds),
            max_slots=args.max_slots,
        )
        while True:
            now = datetime.fromisoformat(args.now) if args.now else datetime.now(timezone.utc)
            for result in scheduler.run_due(now=now):
                print(_json(asdict(result)), flush=True)
            if not args.serve:
                break
            sleep(args.poll_seconds)
    finally:
        store.close()
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="sose",
        description="Synthetic Operational System Engine",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    recovery = subparsers.add_parser(
        "composition-recovery",
        help="Run deterministic PC6 catch-up once (cron) or as a recurring worker.",
    )
    recovery.add_argument("--sqlite", required=True, help="Authoritative SQLite DB path.")
    recovery.add_argument("--owner-id", required=True)
    recovery.add_argument("--start-at", required=True, help="ISO 8601 timezone-aware anchor.")
    recovery.add_argument("--interval-seconds", type=int, required=True)
    recovery.add_argument("--max-slots", type=int, default=16)
    recovery.add_argument("--max-actions", type=int, default=16)
    recovery.add_argument("--now", help="ISO 8601 test/replay time (one-shot only).")
    recovery.add_argument("--serve", action="store_true", help="Poll continuously.")
    recovery.add_argument("--poll-seconds", type=float, default=5.0)
    recovery.set_defaults(handler=_cmd_composition_recovery)

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

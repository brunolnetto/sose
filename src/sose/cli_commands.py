"""Subcommand handlers of the public ``sose`` CLI, split from ``sose.cli``."""

from __future__ import annotations

import argparse
from dataclasses import asdict
import json
from datetime import datetime

from sose.jobs.config import load_sose_config
from sose.jobs.config_edit import (
    describe_domain_config,
    parse_cli_value,
    set_domain_parameter,
)
from sose.jobs.doctor import inspect_job_file_health
from sose.jobs.factory import build_job_from_file
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

from sose.cli_validation import (
    _close_persistence, _json, _recovery_resource_policies, _recovery_store, _validate_config,
)


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
        position = persistence.simulation_position(owner_id=config.job.id)
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

    if args.serve and args.now is not None:
        raise ValueError("--now is only supported for one-shot execution")
    if args.serve and args.poll_seconds <= 0:
        raise ValueError("--poll-seconds must be positive")
    start_at = datetime.fromisoformat(args.start_at)
    if start_at.tzinfo is None:   # fail before connecting to the store
        raise ValueError("start_at must be timezone-aware")
    policies = _recovery_resource_policies(args)
    store = _recovery_store(args)
    try:
        runner = TradingCustomerRecoveryRunner(
            persistence=store,
            owner_id=args.owner_id,
            correlation_id=args.correlation_id,
            organization_owners={args.correlation_id: args.organization_id},
            max_actions=args.max_actions,
            resource_policies=policies,
            resource_organizations={args.correlation_id: args.organization_id},
        )
        scheduler = RecoverySchedule(
            runner=runner,
            start_at=start_at,
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


def _cmd_migrate_owners(args: argparse.Namespace) -> int:
    """Offline owner-mapping migration of a SOSE <= 0.8 SQLite database (ADR-0009)."""
    from pathlib import Path

    from sose.persistence.owner_migration import migrate_unowned_sqlite

    report = migrate_unowned_sqlite(args.sqlite, args.output, owner_id=args.owner)
    text = report.to_json()
    if args.report:
        Path(args.report).write_text(text + "\n", encoding="utf-8")
    print(text)
    return 0

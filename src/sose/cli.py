from __future__ import annotations

import argparse
from dataclasses import asdict
import json
from pathlib import Path
import sys

from sose.examples.catalog import builtin_catalog
from sose.jobs.config import load_sose_config
from sose.jobs.factory import build_job_from_file
from sose.persistence.registry import builtin_persistence_registry


def _json(value: object) -> str:
    return json.dumps(value, indent=2, sort_keys=True, default=str)


def _close_persistence(persistence) -> None:
    close = getattr(persistence, "close", None)
    if callable(close):
        close()


def _validate_config(path: str | Path) -> dict[str, object]:
    config, _ = load_sose_config(path)
    domains = builtin_catalog()
    definition = domains.get(config.domain.name)
    resolved = definition.parse_config(config.domain.parameters)

    persistence_names = builtin_persistence_registry().names()
    if config.persistence.adapter not in persistence_names:
        raise KeyError(
            f"unknown persistence adapter: {config.persistence.adapter}"
        )
    if config.runtime.backend != "simpy":
        raise KeyError(f"unknown runtime backend: {config.runtime.backend}")

    return {
        "job_id": config.job.id,
        "domain": definition.name,
        "domain_parameters": resolved.model_dump(mode="json"),
        "persistence": config.persistence.adapter,
        "runtime_backend": config.runtime.backend,
    }


def _cmd_validate(args: argparse.Namespace) -> int:
    validated = _validate_config(args.config)
    print(_json(validated))
    return 0


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
        _close_persistence(job.persistence)


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
            from sose.domain.config import DomainDefinition
            from sose.jobs.runner import SimulationJob

            definition = builtin_catalog().get(state.domain_name)
            job = SimulationJob(
                job_id=state.job_id,
                definition=definition,
                persistence=persistence,
                backend_factory=lambda origin: None,
            )
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


def _cmd_domains(args: argparse.Namespace) -> int:
    catalog = builtin_catalog()
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


def _cmd_persistence(args: argparse.Namespace) -> int:
    registry = builtin_persistence_registry()
    print(_json({"adapters": list(registry.names())}))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="sose",
        description="Synthetic Operational System Engine",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    validate = subparsers.add_parser(
        "validate",
        help="Validate a sose.toml file and domain parameters.",
    )
    validate.add_argument("--config", default="sose.toml")
    validate.set_defaults(handler=_cmd_validate)

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

    inspect = subparsers.add_parser(
        "inspect",
        help="Inspect durable job checkpoint without advancing it.",
    )
    inspect.add_argument("--config", default="sose.toml")
    inspect.set_defaults(handler=_cmd_inspect)

    domains = subparsers.add_parser(
        "domains",
        help="List builtin executable domains.",
    )
    domains.set_defaults(handler=_cmd_domains)

    persistence = subparsers.add_parser(
        "persistence",
        help="List builtin persistence adapters.",
    )
    persistence.set_defaults(handler=_cmd_persistence)

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

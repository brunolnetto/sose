"""``sose validate`` config checks and JSON helpers, split from ``sose.cli``."""

from __future__ import annotations

import json
from pathlib import Path

from sose.jobs.config import load_sose_config
from sose.jobs.storage import build_storage_plan
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


def _recovery_resource_policies(args):
    """Load ``--resource-policies``: {intent name: ResourcePoolContract fields}."""
    if args.resource_policies is None:
        return None
    if args.postgres_dsn is None:
        raise ValueError("--resource-policies requires --postgres-dsn")
    from sose.core.resource_identity import ResourcePoolContract

    raw = json.loads(Path(args.resource_policies).read_text(encoding="utf-8"))
    if not isinstance(raw, dict) or not raw:
        raise ValueError("--resource-policies must be a non-empty JSON object")
    return {
        str(intent): ResourcePoolContract(**{
            **fields, "instance_ids": tuple(fields.get("instance_ids", ())),
        })
        for intent, fields in raw.items()
    }


def _recovery_store(args):
    """Open the authoritative store selected by ``--sqlite`` / ``--postgres-dsn``."""
    if args.postgres_dsn is not None:
        from sose.persistence.postgres import PostgresPersistence

        return PostgresPersistence(args.postgres_dsn, namespace=args.namespace)
    from sose.persistence.sqlite_incremental import SQLiteIncrementalPersistence

    return SQLiteIncrementalPersistence(args.sqlite)

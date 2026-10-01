from __future__ import annotations

from pathlib import Path

from sose.jobs.config import SOSEConfig, load_sose_config
from sose.jobs.runner import SimulationJob
from sose.persistence.registry import (
    PersistenceRegistry,
    builtin_persistence_registry,
)
from sose.sinks.registry import SinkRegistry, builtin_sink_registry
from sose.jobs.storage import build_storage_plan


def _backend_factory(name: str):
    if name == "simpy":
        try:
            from sose.backends.simpy import SimPyBackend
        except ModuleNotFoundError as exc:
            raise RuntimeError(
                "runtime backend 'simpy' requires installing the 'simpy' extra"
            ) from exc
        return lambda origin: SimPyBackend(origin=origin)
    raise KeyError(f"unknown runtime backend: {name}")


def _domain_warehouse(config: SOSEConfig, base_dir: Path):
    section = config.domain_warehouse
    if section is None:
        return None
    options = dict(section.options)
    if section.adapter == "sqlite":
        from sose.domain.sqlite import SQLiteDomainWarehouse
        raw_path = options.pop("path", "state/domain.sqlite3")
        if not isinstance(raw_path, str) or not raw_path:
            raise ValueError("sqlite DomainWarehouse path must be a non-empty string")
        path = Path(raw_path)
        if not path.is_absolute():
            path = base_dir / path
        if options:
            raise ValueError(f"unknown sqlite DomainWarehouse options: {sorted(options)}")
        return SQLiteDomainWarehouse(path)
    if section.adapter == "postgres":
        try:
            from sose.domain.postgres import PostgresDomainWarehouse
        except ModuleNotFoundError as exc:
            raise RuntimeError(
                "DomainWarehouse adapter 'postgres' requires installing the 'postgres' extra"
            ) from exc
        import os
        dsn = options.pop("dsn", None)
        dsn_env = options.pop("dsn_env", None)
        namespace = options.pop("namespace", "sose_domain")
        for name, value in (("dsn", dsn), ("dsn_env", dsn_env)):
            if value is not None and (not isinstance(value, str) or not value):
                raise ValueError(
                    f"postgres DomainWarehouse {name} must be a non-empty string"
                )
        if not isinstance(namespace, str) or not namespace:
            raise ValueError(
                "postgres DomainWarehouse namespace must be a non-empty string"
            )
        if dsn is None and dsn_env is not None:
            dsn = os.environ.get(dsn_env)
        if options:
            raise ValueError(f"unknown postgres DomainWarehouse options: {sorted(options)}")
        if not dsn:
            raise ValueError("postgres DomainWarehouse requires dsn or dsn_env")
        return PostgresDomainWarehouse(dsn, namespace=namespace)
    raise KeyError(f"unknown DomainWarehouse adapter: {section.adapter}")


def build_job_from_config(
    config: SOSEConfig,
    *,
    base_dir: Path,
    persistence_registry: PersistenceRegistry | None = None,
    sink_registry: SinkRegistry | None = None,
) -> SimulationJob:
    # Import the builtin examples only when a declarative job is actually
    # constructed. The stable sose.api facade must stay importable without
    # optional execution backends and without pulling the example graph into
    # module initialization.
    from sose.examples.catalog import builtin_catalog

    domains = builtin_catalog()
    definition = domains.get(config.domain.name)
    registry = persistence_registry or builtin_persistence_registry()
    sink_adapters = sink_registry or builtin_sink_registry()
    storage = build_storage_plan(
        config,
        persistence_registry=registry,
        sink_registry=sink_adapters,
    )
    persistence = storage.create_authoritative(
        registry=registry,
        base_dir=base_dir,
    )
    warehouse = None
    try:
        sink_bindings = storage.create_sink_bindings(
            registry=sink_adapters,
            base_dir=base_dir,
        )
        warehouse = _domain_warehouse(config, base_dir)
        job = SimulationJob(
            job_id=config.job.id,
            definition=definition,
            persistence=persistence,
            backend_factory=_backend_factory(config.runtime.backend),
            ticks_per_trigger=config.job.ticks_per_trigger,
            max_ticks_per_trigger=config.job.max_ticks_per_trigger,
            sink_bindings=sink_bindings,
            domain_warehouse=warehouse,
        )
        job.initialize(config.domain.parameters)
        return job
    except Exception:
        # Cleanup is best-effort here: never mask the construction error, and
        # always attempt both independently owned stores.
        for store in (warehouse, persistence):
            if store is None:
                continue
            close = getattr(store, "close", None)
            if callable(close):
                try:
                    close()
                except Exception:
                    pass
        raise


def build_job_from_file(
    path: str | Path = "sose.toml",
    *,
    persistence_registry: PersistenceRegistry | None = None,
    sink_registry: SinkRegistry | None = None,
) -> SimulationJob:
    config, base_dir = load_sose_config(path)
    return build_job_from_config(
        config,
        base_dir=base_dir,
        persistence_registry=persistence_registry,
        sink_registry=sink_registry,
    )

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
    sink_bindings = storage.create_sink_bindings(
        registry=sink_adapters,
        base_dir=base_dir,
    )

    job = SimulationJob(
        job_id=config.job.id,
        definition=definition,
        persistence=persistence,
        backend_factory=_backend_factory(config.runtime.backend),
        ticks_per_trigger=config.job.ticks_per_trigger,
        max_ticks_per_trigger=config.job.max_ticks_per_trigger,
        sink_bindings=sink_bindings,
    )
    job.initialize(config.domain.parameters)
    return job


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

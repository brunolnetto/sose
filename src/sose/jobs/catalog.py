from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from sose.jobs.config import (
    JobSection,
    PersistenceSection,
    SOSECatalogConfig,
    SOSEConfig,
    load_sose_catalog_config,
)
from sose.jobs.factory import build_job_from_config
from sose.jobs.runner import SimulationJob
from sose.persistence.registry import PersistenceRegistry
from sose.sinks.registry import SinkRegistry


@dataclass(slots=True)
class SimulationJobCatalog:
    """Resolved durable jobs sharing one physical Engine Store backend."""

    _jobs: dict[str, SimulationJob]

    def ids(self) -> tuple[str, ...]:
        return tuple(sorted(self._jobs))

    def get(self, job_id: str) -> SimulationJob:
        try:
            return self._jobs[job_id]
        except KeyError as exc:
            raise KeyError(f"unknown catalog job: {job_id}") from exc

    def jobs(self) -> tuple[SimulationJob, ...]:
        return tuple(self._jobs[job_id] for job_id in self.ids())

    def close(self) -> None:
        first_error: Exception | None = None
        for job in self.jobs():
            try:
                job.close()
            except Exception as exc:
                if first_error is None:
                    first_error = exc
        if first_error is not None:
            raise first_error

    def __enter__(self) -> "SimulationJobCatalog":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()


def build_job_catalog_from_config(
    config: SOSECatalogConfig,
    *,
    base_dir: Path,
    persistence_registry: PersistenceRegistry | None = None,
    sink_registry: SinkRegistry | None = None,
) -> SimulationJobCatalog:
    jobs: dict[str, SimulationJob] = {}
    try:
        for item in config.jobs:
            engine_options = dict(config.engine_store.options)
            engine_options["namespace"] = item.resolved_engine_namespace
            engine_store = PersistenceSection(
                adapter=config.engine_store.adapter,
                require=list(config.engine_store.require),
                options=engine_options,
            )
            single = SOSEConfig(
                domain=item.domain,
                engine_store=engine_store,
                domain_store=item.domain_store,
                runtime=config.runtime,
                sinks=item.sinks,
                job=JobSection(
                    id=item.id,
                    ticks_per_trigger=item.ticks_per_trigger,
                    max_ticks_per_trigger=item.max_ticks_per_trigger,
                ),
            )
            jobs[item.id] = build_job_from_config(
                single,
                base_dir=base_dir,
                persistence_registry=persistence_registry,
                sink_registry=sink_registry,
            )
        return SimulationJobCatalog(jobs)
    except Exception:
        for job in jobs.values():
            try:
                job.close()
            except Exception:
                pass
        raise


def build_job_catalog_from_file(
    path: str | Path = "sose-catalog.toml",
    *,
    persistence_registry: PersistenceRegistry | None = None,
    sink_registry: SinkRegistry | None = None,
) -> SimulationJobCatalog:
    config, base_dir = load_sose_catalog_config(path)
    return build_job_catalog_from_config(
        config,
        base_dir=base_dir,
        persistence_registry=persistence_registry,
        sink_registry=sink_registry,
    )

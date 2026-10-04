from __future__ import annotations

import builtins
from types import SimpleNamespace

import pytest

from sose.backends.simpy import SimPyBackend
from sose.examples.catalog import builtin_catalog
from sose.jobs import factory as jobs_factory
from sose.jobs.config import DomainWarehouseSection
from sose.jobs.model import SimulationJobState
from sose.jobs.runner import SimulationJob
from sose.persistence.memory import MemoryPersistence


def _tutorial_job(persistence: MemoryPersistence) -> SimulationJob:
    definition = builtin_catalog().get("tutorial_job")
    return SimulationJob(
        job_id="coverage-job",
        definition=definition,
        persistence=persistence,
        backend_factory=lambda now: SimPyBackend(origin=now),
    )


def test_domain_warehouse_section_rejects_unknown_adapter(tmp_path):
    section = DomainWarehouseSection.model_validate(
        {"adapter": "unknown", "options": {}}
    )

    with pytest.raises(KeyError, match="unknown DomainWarehouse adapter"):
        jobs_factory._domain_warehouse_section(section, tmp_path)


def test_postgres_domain_warehouse_requires_postgres_extra(monkeypatch):
    original_import = builtins.__import__

    def _failing_import(name, globals=None, locals=None, fromlist=(), level=0):
        if name == "sose.domain.postgres":
            raise ModuleNotFoundError(name)
        return original_import(name, globals, locals, fromlist, level)

    monkeypatch.setattr(builtins, "__import__", _failing_import)

    with pytest.raises(RuntimeError, match="requires installing the 'postgres' extra"):
        jobs_factory._postgres_domain_warehouse({"dsn": "postgresql://example"})


def test_initialize_finishes_existing_pending_state():
    persistence = MemoryPersistence()
    job = _tutorial_job(persistence)
    config = job.definition.default_config()
    pending = SimulationJobState(
        job_id=job.job_id,
        domain_name=job.definition.name,
        config_json=config.model_dump_json(),
        config_revision=1,
        status="ready",
        initialized=False,
        logical_time=config.start_at,
        next_tick=0,
    )
    with persistence.transaction() as uow:
        uow.save_job_state(pending)

    state = job.initialize()

    assert state.initialized is True


def test_job_close_skips_stores_without_callable_close():
    job = _tutorial_job(MemoryPersistence())
    job.domain_warehouse = SimpleNamespace(close=123)
    job.persistence = SimpleNamespace(close=456)

    job.close()


def test_build_reconcile_runtime_skips_run_until_when_not_callable():
    job = _tutorial_job(MemoryPersistence())
    config = job.definition.default_config()
    running = SimulationJobState(
        job_id=job.job_id,
        domain_name=job.definition.name,
        config_json=config.model_dump_json(),
        config_revision=1,
        status="ready",
        initialized=True,
        logical_time=config.start_at,
        next_tick=3,
    )
    job.persistence = SimpleNamespace(
        simulation_position=lambda: SimpleNamespace(
            logical_time=config.start_at,
            logical_tick=running.next_tick,
        )
    )
    job._build_runtime_for_tick = lambda *_args, **_kwargs: (
        SimpleNamespace(clock=SimpleNamespace(now=config.start_at)),
        object(),
        object(),
        None,
    )

    rebuilt = job._build_reconcile_runtime(config, running=running)

    assert rebuilt[0] is running

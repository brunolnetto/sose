from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

import sose.jobs.catalog as catalog_module
import sose.jobs.persistent as persistent_module
from sose.jobs.catalog import (
    SimulationJobCatalog,
    build_job_catalog_from_config,
    build_job_catalog_from_file,
)
from sose.jobs.config import (
    CatalogJobSection,
    DomainSection,
    JobSection,
    PersistenceSection,
    SOSECatalogConfig,
    engine_namespace_for_job,
)
from sose.jobs.persistent import PersistentJobRunner
from sose.persistence.sqlite_incremental import StaleWriterError, WriterLease


class _ClosableJob:
    def __init__(self, *, error: Exception | None = None) -> None:
        self.error = error
        self.closed = 0

    def close(self) -> None:
        self.closed += 1
        if self.error is not None:
            raise self.error


def test_simulation_job_catalog_collection_and_context_contract():
    b = _ClosableJob()
    a = _ClosableJob()
    catalog = SimulationJobCatalog({"b": b, "a": a})

    assert catalog.ids() == ("a", "b")
    assert catalog.jobs() == (a, b)
    assert catalog.get("a") is a
    with pytest.raises(KeyError, match="unknown catalog job"):
        catalog.get("missing")

    with catalog as entered:
        assert entered is catalog

    assert a.closed == 1
    assert b.closed == 1


def test_simulation_job_catalog_closes_every_job_before_reraising_first_error():
    first = _ClosableJob(error=RuntimeError("first"))
    second = _ClosableJob(error=ValueError("second"))
    third = _ClosableJob()

    with pytest.raises(RuntimeError, match="first"):
        SimulationJobCatalog(
            {"first": first, "second": second, "third": third}
        ).close()

    assert first.closed == second.closed == third.closed == 1


def _catalog_config(*jobs: CatalogJobSection, **engine_options) -> SOSECatalogConfig:
    return SOSECatalogConfig(
        engine_store=PersistenceSection(
            adapter="sqlite_incremental",
            options={"path": "engine.sqlite3", **engine_options},
        ),
        jobs=list(jobs),
    )


def _catalog_job(job_id: str, **kwargs) -> CatalogJobSection:
    return CatalogJobSection(
        id=job_id,
        domain=DomainSection(name="producer_consumer"),
        **kwargs,
    )


def test_build_job_catalog_from_config_wires_per_job_namespace(tmp_path, monkeypatch):
    created = []

    def fake_build(config, **kwargs):
        job = _ClosableJob()
        created.append((config, kwargs, job))
        return job

    monkeypatch.setattr(catalog_module, "build_job_from_config", fake_build)
    config = _catalog_config(
        _catalog_job("first-job"),
        _catalog_job("second", engine_namespace="custom_ns"),
    )

    result = build_job_catalog_from_config(
        config,
        base_dir=tmp_path,
        persistence_registry="persistence-registry",
        sink_registry="sink-registry",
    )

    assert result.ids() == ("first-job", "second")
    first_config, first_kwargs, _ = created[0]
    second_config, second_kwargs, _ = created[1]
    assert first_config.engine_store.options["path"] == "engine.sqlite3"
    assert first_config.engine_store.options["namespace"] == engine_namespace_for_job(
        "first-job"
    )
    assert second_config.engine_store.options["namespace"] == "custom_ns"
    assert first_config.job.id == "first-job"
    assert second_config.job.id == "second"
    assert first_kwargs == second_kwargs == {
        "base_dir": tmp_path,
        "persistence_registry": "persistence-registry",
        "sink_registry": "sink-registry",
    }


def test_build_job_catalog_from_config_closes_partial_jobs_on_failure(
    tmp_path, monkeypatch
):
    first = _ClosableJob()
    attempts = 0

    def fake_build(config, **kwargs):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            return first
        raise RuntimeError("build failed")

    monkeypatch.setattr(catalog_module, "build_job_from_config", fake_build)
    config = _catalog_config(_catalog_job("one"), _catalog_job("two"))

    with pytest.raises(RuntimeError, match="build failed"):
        build_job_catalog_from_config(config, base_dir=tmp_path)

    assert first.closed == 1


def test_build_job_catalog_cleanup_ignores_close_error_and_preserves_build_error(
    tmp_path, monkeypatch
):
    first = _ClosableJob(error=RuntimeError("close failed"))
    attempts = 0

    def fake_build(config, **kwargs):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            return first
        raise ValueError("original build failure")

    monkeypatch.setattr(catalog_module, "build_job_from_config", fake_build)
    config = _catalog_config(_catalog_job("one"), _catalog_job("two"))

    with pytest.raises(ValueError, match="original build failure"):
        build_job_catalog_from_config(config, base_dir=tmp_path)

    assert first.closed == 1


def test_build_job_catalog_from_file_delegates_loaded_config(tmp_path, monkeypatch):
    path = tmp_path / "catalog.toml"
    config = _catalog_config(_catalog_job("one"))
    expected = SimulationJobCatalog({})

    monkeypatch.setattr(
        catalog_module,
        "load_sose_catalog_config",
        lambda supplied: (config, tmp_path),
    )
    captured = {}

    def fake_build(config_arg, **kwargs):
        captured["config"] = config_arg
        captured.update(kwargs)
        return expected

    monkeypatch.setattr(catalog_module, "build_job_catalog_from_config", fake_build)

    assert build_job_catalog_from_file(
        path,
        persistence_registry="p",
        sink_registry="s",
    ) is expected
    assert captured == {
        "config": config,
        "base_dir": tmp_path,
        "persistence_registry": "p",
        "sink_registry": "s",
    }


def test_engine_namespace_normalization_is_stable_and_sql_safe():
    assert engine_namespace_for_job("Orders EU") == engine_namespace_for_job("Orders EU")
    assert engine_namespace_for_job("123").startswith("job_123_")
    assert engine_namespace_for_job("***").startswith("job__")
    assert len(engine_namespace_for_job("x" * 200)) <= 40
    assert engine_namespace_for_job("ABC").startswith("abc_")


def test_job_section_rejects_invalid_trigger_bounds():
    with pytest.raises(ValueError, match="ticks_per_trigger cannot exceed"):
        JobSection(id="job", ticks_per_trigger=2, max_ticks_per_trigger=1)


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"id": ""}, "catalog job id cannot be empty"),
        (
            {"id": "job", "ticks_per_trigger": 2, "max_ticks_per_trigger": 1},
            "ticks_per_trigger cannot exceed",
        ),
        (
            {"id": "job", "engine_namespace": "bad-name"},
            "engine_namespace must be a SQL-safe identifier",
        ),
    ],
)
def test_catalog_job_section_validates_contract(kwargs, message):
    with pytest.raises(ValueError, match=message):
        CatalogJobSection(
            domain=DomainSection(name="producer_consumer"),
            **kwargs,
        )

    explicit = _catalog_job("job", engine_namespace="explicit_ns")
    assert explicit.resolved_engine_namespace == "explicit_ns"
    assert _catalog_job("generated").resolved_engine_namespace == engine_namespace_for_job(
        "generated"
    )


def test_catalog_config_rejects_duplicate_ids():
    with pytest.raises(ValueError, match="job ids must be unique"):
        _catalog_config(_catalog_job("same"), _catalog_job("same"))


def test_catalog_config_rejects_duplicate_engine_namespaces():
    with pytest.raises(ValueError, match="namespaces must be unique"):
        _catalog_config(
            _catalog_job("one", engine_namespace="same_ns"),
            _catalog_job("two", engine_namespace="same_ns"),
        )


def test_catalog_config_requires_supported_shared_engine_store():
    with pytest.raises(ValueError, match="currently require"):
        SOSECatalogConfig(
            engine_store=PersistenceSection(adapter="memory"),
            jobs=[_catalog_job("one")],
        )


def test_catalog_config_rejects_global_namespace_option():
    with pytest.raises(ValueError, match="namespace is job-specific"):
        SOSECatalogConfig(
            engine_store=PersistenceSection(
                adapter="sqlite_incremental",
                options={"path": "engine.sqlite3", "namespace": "global"},
            ),
            jobs=[_catalog_job("one")],
        )


def test_catalog_config_rejects_in_memory_sqlite_shared_store():
    with pytest.raises(ValueError, match="file-backed path"):
        SOSECatalogConfig(
            engine_store=PersistenceSection(
                adapter="sqlite_incremental",
                options={"path": ":memory:"},
            ),
            jobs=[_catalog_job("one")],
        )


def test_catalog_config_requires_lowercase_explicit_sqlite_namespaces():
    with pytest.raises(ValueError, match="must be lowercase"):
        _catalog_config(_catalog_job("one", engine_namespace="MixedCase"))


class _OwnershipPersistence:
    def __init__(self, *, failures: int = 0) -> None:
        self.epoch = 4
        self.failures = failures
        self.claim_calls = []

    def writer_epoch(self) -> int:
        return self.epoch

    def claim_writer(self, owner_id: str, *, expected_epoch: int):
        self.claim_calls.append((owner_id, expected_epoch))
        if self.failures:
            self.failures -= 1
            self.epoch += 1
            raise StaleWriterError("race")
        self.epoch += 1
        return WriterLease(owner_id, self.epoch)

    def transaction(self, *, owner_epoch=None):
        raise AssertionError("not needed by these unit tests")


def _runner_job(persistence):
    return SimpleNamespace(
        job_id="job",
        definition="definition",
        persistence=persistence,
        backend_factory="backend-factory",
        ticks_per_trigger=2,
        max_ticks_per_trigger=5,
        sink_bindings=("sink",),
    )


@pytest.mark.parametrize(
    ("owner_id", "claim_retries", "message"),
    [
        ("", 3, "owner_id cannot be empty"),
        ("worker", 0, "claim_retries must be >= 1"),
    ],
)
def test_persistent_runner_validates_constructor(owner_id, claim_retries, message):
    with pytest.raises(ValueError, match=message):
        PersistentJobRunner(
            _runner_job(_OwnershipPersistence()),
            owner_id=owner_id,
            claim_retries=claim_retries,
        )


@pytest.mark.parametrize("missing", ["writer_epoch", "claim_writer"])
def test_persistent_runner_requires_ownership_capabilities(missing):
    persistence = _OwnershipPersistence()
    setattr(persistence, missing, None)
    with pytest.raises(TypeError, match=f"{missing} is missing"):
        PersistentJobRunner(_runner_job(persistence), owner_id="worker")


def test_persistent_runner_retries_compare_and_swap_claims():
    persistence = _OwnershipPersistence(failures=2)
    runner = PersistentJobRunner(
        _runner_job(persistence),
        owner_id="worker",
        claim_retries=3,
    )

    lease = runner._claim()

    assert lease.epoch == 7
    assert persistence.claim_calls == [
        ("worker", 4),
        ("worker", 5),
        ("worker", 6),
    ]


def test_persistent_runner_reraises_last_claim_failure():
    persistence = _OwnershipPersistence(failures=3)
    runner = PersistentJobRunner(
        _runner_job(persistence),
        owner_id="worker",
        claim_retries=3,
    )

    with pytest.raises(StaleWriterError, match="race"):
        runner._claim()


class _FakeFencedJob:
    instances = []

    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.instances.append(self)

    def run_tick(self, **kwargs):
        return ("tick", kwargs)

    def run_trigger(self, **kwargs):
        return ("trigger", kwargs)

    def run_scheduled_trigger(self, **kwargs):
        return ("scheduled", kwargs)


def test_persistent_runner_builds_fenced_job_and_wraps_all_entrypoints(monkeypatch):
    persistence = _OwnershipPersistence()
    job = _runner_job(persistence)
    monkeypatch.setattr(persistent_module, "SimulationJob", _FakeFencedJob)

    runner = PersistentJobRunner(job, owner_id="worker")

    tick = runner.run_tick(trigger_id="t")
    trigger = runner.run_trigger(trigger_id="batch")
    scheduled = runner.run_scheduled_trigger(
        scheduled_for=datetime(2026, 1, 1, tzinfo=timezone.utc)
    )

    assert tick.owner_id == trigger.owner_id == scheduled.owner_id == "worker"
    assert tick.result == ("tick", {"trigger_id": "t"})
    assert trigger.result == ("trigger", {"trigger_id": "batch"})
    assert scheduled.result[0] == "scheduled"
    assert [tick.epoch, trigger.epoch, scheduled.epoch] == [5, 6, 7]

    instance = _FakeFencedJob.instances[0]
    assert instance.kwargs["job_id"] == "job"
    assert instance.kwargs["definition"] == "definition"
    assert instance.kwargs["backend_factory"] == "backend-factory"
    assert instance.kwargs["ticks_per_trigger"] == 2
    assert instance.kwargs["max_ticks_per_trigger"] == 5
    assert instance.kwargs["sink_bindings"] == ("sink",)
    assert instance.kwargs["persistence"].lease.epoch == 5

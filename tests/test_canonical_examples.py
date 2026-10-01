from __future__ import annotations

import pytest

from sose.examples.catalog import builtin_catalog
from sose.jobs.scaffold import render_sose_toml
from sose.jobs.runner import SimulationJob
from sose.backends.simpy import SimPyBackend
from sose.persistence.sqlite_incremental import SQLiteIncrementalPersistence
from sose.persistence.memory import MemoryPersistence
from tests.support.behavioral_conformance import assert_conservation, assert_precedence, assert_resource_exclusive, assert_store_capacity, assert_unique_consumption


CANONICALS = (
    "producer_consumer",
    "sleeping_barber",
    "dining_philosophers",
    "readers_writers",
    "job_shop",
)


@pytest.mark.parametrize("name", CANONICALS)
def test_canonical_is_wired_as_regular_domain(name):
    definition = builtin_catalog().get(name)
    config = definition.default_config()
    assert config.start_at is not None
    rendered = render_sose_toml(definition)
    assert f'name = "{name}"' in rendered
    assert "[domain.parameters]" in rendered
    assert "[engine_store]" in rendered
    assert "[runtime]" not in rendered


def test_catalog_contains_business_and_canonical_examples():
    names = set(builtin_catalog().names())
    assert set(CANONICALS) <= names
    assert {"mro", "manufacturing", "hospitals"} <= names


@pytest.mark.parametrize("name", CANONICALS)
def test_canonical_executes_across_fresh_process_restart(tmp_path, name):
    definition = builtin_catalog().get(name)
    path = tmp_path / f"{name}.sqlite3"
    job_id = f"canonical-{name}"

    persistence = SQLiteIncrementalPersistence(path)
    job = SimulationJob(
        job_id=job_id,
        definition=definition,
        persistence=persistence,
        backend_factory=lambda origin: SimPyBackend(origin=origin),
    )
    job.initialize()
    job.run_tick(trigger_id=f"{name}:1")
    persistence.close()

    persistence = SQLiteIncrementalPersistence(path)
    restarted = SimulationJob(
        job_id=job_id,
        definition=definition,
        persistence=persistence,
        backend_factory=lambda origin: SimPyBackend(origin=origin),
    )
    result = restarted.run_tick(trigger_id=f"{name}:2")
    assert result.logical_tick == 2
    assert restarted.state().status == "ready"
    persistence.close()


def test_readers_writers_represents_concurrent_read_capacity():
    definition = builtin_catalog().get("readers_writers")
    config = definition.default_config()
    persistence = MemoryPersistence()
    definition.seed(persistence, config)
    resources = {item.name: item.capacity for item in persistence.resource_definitions()}
    assert resources["reader_slots"] == config.readers
    assert resources["writer_gate"] == 1


def test_dining_philosophers_has_one_exclusive_fork_per_participant():
    definition = builtin_catalog().get("dining_philosophers")
    config = definition.default_config()
    persistence = MemoryPersistence()
    definition.seed(persistence, config)
    resources = persistence.resource_definitions()
    assert len(resources) == config.participants
    assert all(resource.capacity == 1 for resource in resources)

def test_producer_consumer_preserves_capacity_and_unique_consumption(tmp_path):
    definition = builtin_catalog().get("producer_consumer")
    config = definition.default_config()
    persistence = SQLiteIncrementalPersistence(tmp_path / "producer.sqlite3")
    job = SimulationJob(job_id="producer-invariants", definition=definition,
        persistence=persistence, backend_factory=lambda origin: SimPyBackend(origin=origin))
    job.initialize()
    for tick in range(1, 5):
        job.run_tick(trigger_id=f"pc:{tick}")
        assert_store_capacity(persistence, "buffer", config.capacity)
        assert_unique_consumption(persistence, "buffer")
    consumed = len(persistence.store_get_results())
    buffered = len(persistence.store_items())
    pending = len(persistence.store_put_intents())
    assert_conservation(total=config.participants,
        buckets={"consumed": consumed, "buffered": buffered, "pending": pending})
    persistence.close()


def test_sleeping_barber_records_abandonment_durably(tmp_path):
    definition = builtin_catalog().get("sleeping_barber")
    config = definition.default_config()
    persistence = SQLiteIncrementalPersistence(tmp_path / "barber.sqlite3")
    job = SimulationJob(job_id="barber-invariants", definition=definition,
        persistence=persistence, backend_factory=lambda origin: SimPyBackend(origin=origin))
    job.initialize()
    job.run_tick(trigger_id="barber:1")
    persistence.close()
    persistence = SQLiteIncrementalPersistence(tmp_path / "barber.sqlite3")
    abandoned = [i for i in persistence.store_items() if i.store_name == "abandoned"]
    expected = max(0, config.participants - config.waiting_chairs - config.capacity)
    assert len(abandoned) == expected
    persistence.close()


def test_coordination_canonicals_preserve_exclusive_resources(tmp_path):
    for name in ("dining_philosophers", "readers_writers"):
        definition = builtin_catalog().get(name)
        persistence = SQLiteIncrementalPersistence(tmp_path / f"{name}.sqlite3")
        job = SimulationJob(job_id=f"{name}-semantics", definition=definition,
            persistence=persistence, backend_factory=lambda origin: SimPyBackend(origin=origin))
        job.initialize()
        for tick in range(1, 10):
            job.run_tick(trigger_id=f"{name}:{tick}")
            for resource in persistence.resource_definitions():
                if resource.capacity == 1:
                    assert_resource_exclusive(persistence, resource.name)
        persistence.close()


def test_writer_never_owns_gate_while_reader_is_active(tmp_path):
    definition = builtin_catalog().get("readers_writers")
    persistence = SQLiteIncrementalPersistence(tmp_path / "rw.sqlite3")
    job = SimulationJob(job_id="rw-exclusion", definition=definition,
        persistence=persistence, backend_factory=lambda origin: SimPyBackend(origin=origin))
    job.initialize()
    for tick in range(1, 10):
        job.run_tick(trigger_id=f"rw:{tick}")
        reservations = persistence.resource_reservations()
        readers_active = any(r.resource_name == "reader_slots" for r in reservations)
        writer_active = any(r.request_id.startswith("writer-") for r in reservations)
        assert not (readers_active and writer_active)
    persistence.close()

def test_job_shop_enforces_multi_operation_precedence(tmp_path):
    definition = builtin_catalog().get("job_shop")
    config = definition.default_config()
    persistence = SQLiteIncrementalPersistence(tmp_path / "job-shop-v2.sqlite3")
    job = SimulationJob(job_id="job-shop-v2", definition=definition,
        persistence=persistence, backend_factory=lambda origin: SimPyBackend(origin=origin))
    job.initialize()
    for tick in range(1, 30):
        job.run_tick(trigger_id=f"job-shop:{tick}")
        for machine in persistence.resource_definitions():
            assert_resource_exclusive(persistence, machine.name)
        completed = [
            item.item_id.removesuffix("-done")
            for item in sorted(persistence.store_items(), key=lambda item: item.sequence)
            if item.store_name == "completed_operations"
        ]
        edges = [
            (f"job-{index}-op-0", f"job-{index}-op-1")
            for index in range(config.participants)
        ]
        assert_precedence(completed, edges)
        if persistence.entity("canonical_case", job.state().bootstrap_state.id).state == "completed":
            break
    assert all(f"job-{index}-op-1" in completed for index in range(config.participants))
    persistence.close()

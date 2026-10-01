from __future__ import annotations

import pytest

from sose.backends.simpy import SimPyBackend
from sose.examples.catalog import builtin_catalog
from sose.jobs.runner import SimulationJob
from sose.persistence.sqlite_incremental import SQLiteIncrementalPersistence


CANONICALS = builtin_catalog().names(kind="canonical")


def backend(origin):
    return SimPyBackend(origin=origin)


def snapshot(persistence):
    return {
        "job": tuple(
            (s.job_id, s.next_tick, s.run_count, s.status, s.phase)
            for s in persistence.job_states()
        ),
        "resources": tuple(
            sorted((r.resource_name, r.reservation_id, r.request_id)
                   for r in persistence.resource_reservations())
        ),
        "demands": tuple(
            sorted((d.resource_name, d.request_id)
                   for d in persistence.resource_demands())
        ),
        "store_results": tuple(
            sorted((r.store_name, r.request_id, r.item.item_id)
                   for r in persistence.store_get_results())
        ),
    }


@pytest.mark.parametrize("name", CANONICALS)
def test_continuous_and_restarted_execution_are_equivalent(tmp_path, name):
    definition = builtin_catalog().get(name)
    continuous_path = tmp_path / f"{name}-continuous.sqlite3"
    restarted_path = tmp_path / f"{name}-restarted.sqlite3"

    continuous = SQLiteIncrementalPersistence(continuous_path)
    continuous_job = SimulationJob(
        job_id=f"{name}-equivalence",
        definition=definition,
        persistence=continuous,
        backend_factory=backend,
    )
    continuous_job.initialize()
    for tick in range(1, 5):
        continuous_job.run_tick(trigger_id=f"{name}:{tick}")
    continuous_snapshot = snapshot(continuous)
    continuous.close()

    first = SQLiteIncrementalPersistence(restarted_path)
    first_job = SimulationJob(
        job_id=f"{name}-equivalence",
        definition=definition,
        persistence=first,
        backend_factory=backend,
    )
    first_job.initialize()
    for tick in range(1, 3):
        first_job.run_tick(trigger_id=f"{name}:{tick}")
    first.close()

    resumed = SQLiteIncrementalPersistence(restarted_path)
    resumed_job = SimulationJob(
        job_id=f"{name}-equivalence",
        definition=definition,
        persistence=resumed,
        backend_factory=backend,
    )
    for tick in range(3, 5):
        resumed_job.run_tick(trigger_id=f"{name}:{tick}")
    restarted_snapshot = snapshot(resumed)
    resumed.close()

    assert restarted_snapshot == continuous_snapshot


def test_catalog_classifies_canonicals_without_changing_default_listing():
    catalog = builtin_catalog()
    assert set(CANONICALS) == {
        "producer_consumer", "sleeping_barber", "dining_philosophers",
        "readers_writers", "job_shop",
    }
    assert len(catalog.names(kind="domain")) == 22
    assert len(catalog.names()) == 27
    assert set(catalog.names(kind="canonical")).isdisjoint(
        catalog.names(kind="domain")
    )

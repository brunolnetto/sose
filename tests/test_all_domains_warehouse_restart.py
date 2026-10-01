from __future__ import annotations

import pytest

from sose.backends.simpy import SimPyBackend
from sose.domain.sqlite import SQLiteDomainWarehouse
from sose.examples.catalog import builtin_catalog
from sose.jobs.runner import SimulationJob
from sose.persistence.sqlite_incremental import SQLiteIncrementalPersistence


def backend(origin):
    return SimPyBackend(origin=origin)


def snapshot(warehouse):
    return tuple(
        (e.entity_type, e.id, e.state, e.version, e.attributes)
        for e in warehouse.entities()
    )


@pytest.mark.parametrize("domain_name", builtin_catalog().names())
def test_every_builtin_domain_continues_after_dual_store_restart(tmp_path, domain_name):
    definition = builtin_catalog().get(domain_name)
    engine_path = tmp_path / f"{domain_name}-engine.db"
    domain_path = tmp_path / f"{domain_name}-domain.db"
    job_id = f"{domain_name}-dual-store-restart"

    engine = SQLiteIncrementalPersistence(engine_path)
    warehouse = SQLiteDomainWarehouse(domain_path)
    first = SimulationJob(
        job_id=job_id,
        definition=definition,
        persistence=engine,
        backend_factory=backend,
        domain_warehouse=warehouse,
    )
    first.initialize()
    first.run_tick(trigger_id=f"{domain_name}:1")
    assert engine.entities() == ()
    before = snapshot(warehouse)
    engine.close()
    warehouse.close()

    engine = SQLiteIncrementalPersistence(engine_path)
    warehouse = SQLiteDomainWarehouse(domain_path)
    restarted = SimulationJob(
        job_id=job_id,
        definition=definition,
        persistence=engine,
        backend_factory=backend,
        domain_warehouse=warehouse,
    )
    restarted.run_tick(trigger_id=f"{domain_name}:2")
    assert engine.entities() == ()
    assert engine.domain_deliveries() == ()
    assert warehouse.entities()
    assert snapshot(warehouse) != ()
    assert before != ()
    engine.close()
    warehouse.close()

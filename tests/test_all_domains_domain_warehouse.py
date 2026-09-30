import pytest

from sose.backends.simpy import SimPyBackend
from sose.domain.warehouse import MemoryDomainWarehouse
from sose.examples.catalog import builtin_catalog
from sose.jobs.runner import SimulationJob
from sose.persistence.memory import MemoryPersistence


@pytest.mark.parametrize("domain_name", builtin_catalog().names())
def test_every_builtin_domain_seeds_without_engine_entity_shadows(domain_name):
    definition = builtin_catalog().get(domain_name)
    engine = MemoryPersistence()
    warehouse = MemoryDomainWarehouse()
    job = SimulationJob(
        job_id=f"warehouse-{domain_name}",
        definition=definition,
        persistence=engine,
        backend_factory=lambda now: SimPyBackend(origin=now),
        domain_warehouse=warehouse,
    )
    job.initialize()
    assert engine.entities() == ()
    assert warehouse.entities(), domain_name
    assert engine.domain_deliveries() == ()

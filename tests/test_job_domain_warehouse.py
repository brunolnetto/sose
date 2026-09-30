from sose.backends.simpy import SimPyBackend
from sose.domain.warehouse import MemoryDomainWarehouse
from sose.examples.catalog import builtin_catalog
from sose.jobs.runner import SimulationJob
from sose.persistence.memory import MemoryPersistence

def test_simulation_job_owns_domain_warehouse_lifecycle():
    definition = builtin_catalog().get('tutorial_job')
    persistence = MemoryPersistence()
    warehouse = MemoryDomainWarehouse()
    job = SimulationJob(
        job_id='warehouse-job',
        definition=definition,
        persistence=persistence,
        backend_factory=lambda now: SimPyBackend(origin=now),
        domain_warehouse=warehouse,
    )
    job.initialize()
    result = job.run_tick()
    assert result.logical_tick == 1
    assert persistence.domain_deliveries() == ()

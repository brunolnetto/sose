from datetime import timedelta

from sose.backends.simpy import SimPyBackend
from sose.examples.itsm.scenarios import staff_shortage_scenario
from sose.examples.itsm.simulation import (
    ORIGIN,
    build_runtime,
    claim_next_incident,
    seed_reference,
    triage_and_queue,
)
from sose.persistence.memory import MemoryPersistence


def test_finite_staff_shortage_preserves_queue_and_recovers():
    persistence = MemoryPersistence()
    entities = seed_reference(persistence)
    context, engine = build_runtime(
        persistence,
        scenarios=(staff_shortage_scenario(),),
    )
    backend = SimPyBackend(origin=ORIGIN)
    engine.rebuild_backend(backend)

    triage_and_queue(
        persistence,
        engine,
        backend,
        incident_id=entities.incident_id,
        sla_delay=timedelta(hours=10),
    )

    engine.advance_tick()
    backend.run_until(context.clock.now)
    assert context.scenarios.attribute("itsm.support.available", True) is False

    assert claim_next_incident(
        persistence,
        engine,
        backend,
        claim_id="shortage",
    ) is None
    assert [
        item.value["incident_id"] for item in persistence.store_items()
    ] == [entities.incident_id]

    for _ in range(4):
        engine.advance_tick()
    backend.run_until(context.clock.now)

    assert context.scenarios.attribute("itsm.support.available", True) is True
    assert claim_next_incident(
        persistence,
        engine,
        backend,
        claim_id="recovered",
    ) == entities.incident_id

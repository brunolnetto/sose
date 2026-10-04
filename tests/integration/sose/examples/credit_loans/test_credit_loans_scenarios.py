from sose.backends.simpy import SimPyBackend
from sose.examples.credit_loans.scenarios import macroeconomic_stress_scenario
from sose.examples.credit_loans.simulation import (
    ORIGIN,
    build_runtime,
    reconcile_underwriting,
    seed_reference,
)
from sose.persistence.memory import MemoryPersistence


def test_macroeconomic_stress_blocks_underwriting_without_stale_capacity():
    persistence = MemoryPersistence()
    entities = seed_reference(persistence)
    context, engine = build_runtime(
        persistence,
        scenarios=(macroeconomic_stress_scenario(),),
    )
    backend = SimPyBackend(origin=ORIGIN)
    engine.rebuild_backend(backend)

    engine.advance_tick()
    backend.run_until(context.clock.now)
    assert context.scenarios.attribute(
        "credit_loans.underwriting.available", True
    ) is False

    assert reconcile_underwriting(
        persistence, engine, backend, entities=entities, approve=True
    ) is False
    assert persistence.resource_demands() == ()
    assert persistence.resource_reservations() == ()

    for _ in range(4):
        engine.advance_tick()
    backend.run_until(context.clock.now)

    assert reconcile_underwriting(
        persistence, engine, backend, entities=entities, approve=True
    )

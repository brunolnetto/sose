from sose.backends.simpy import SimPyBackend
from sose.examples.energy_utilities.scenarios import (
    ORIGIN,
    demand_response_communications_outage,
)
from sose.examples.energy_utilities.simulation import (
    build_runtime,
    reconcile_demand_response,
    schedule_demand_response,
    seed_reference,
)
from sose.persistence.memory import MemoryPersistence


def test_dr_communications_outage_defers_participation_within_event_window():
    persistence = MemoryPersistence()
    entities = seed_reference(persistence)
    context, engine = build_runtime(
        persistence,
        scenarios=(demand_response_communications_outage(),),
    )
    backend = SimPyBackend(origin=ORIGIN)
    engine.rebuild_backend(backend)

    event, participation, start_at, end_at = schedule_demand_response(
        persistence,
        engine,
        backend,
        entities=entities,
        event_key="dr-scenario",
    )

    engine.advance_tick()
    backend.run_until(start_at)
    assert persistence.entity("utility_dr_event", event.id).state == "active"
    assert reconcile_demand_response(
        persistence,
        engine,
        entities=entities,
        event_key="dr-scenario",
    ) is False
    assert persistence.entity(
        "utility_dr_participation",
        participation.id,
    ).state == "eligible"

    for _ in range(2):
        engine.advance_tick()
    backend.run_until(context.clock.now)

    assert reconcile_demand_response(
        persistence,
        engine,
        entities=entities,
        event_key="dr-scenario",
    )
    assert persistence.entity(
        "utility_dr_participation",
        participation.id,
    ).state == "active"

    backend.run_until(end_at)
    assert reconcile_demand_response(
        persistence,
        engine,
        entities=entities,
        event_key="dr-scenario",
    )
    assert persistence.entity("utility_dr_event", event.id).state == "completed"
    assert persistence.entity(
        "utility_dr_participation",
        participation.id,
    ).state == "completed"

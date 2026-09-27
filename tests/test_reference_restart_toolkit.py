from datetime import timedelta

from sose.backends.simpy import SimPyBackend
from sose.examples.aviation.simulation import (
    ORIGIN,
    build_runtime,
    schedule_departure,
    seed_reference,
)
from sose.persistence.memory import MemoryPersistence
from sose.testing.restart import restart_reference_runtime


def test_restart_reference_runtime_rebuilds_at_same_logical_boundary():
    persistence = MemoryPersistence()
    entities = seed_reference(persistence)
    _, engine = build_runtime(persistence)
    backend = SimPyBackend(origin=ORIGIN)
    engine.rebuild_backend(backend)

    due_at = schedule_departure(
        persistence,
        engine,
        backend,
        flight_id=entities.leg1_id,
        delay=timedelta(hours=2),
    )

    rebuilt = restart_reference_runtime(
        persistence,
        build_runtime,
        backend,
        backend_factory=SimPyBackend,
    )

    assert rebuilt.restarted_at == ORIGIN
    assert rebuilt.backend.now == ORIGIN
    assert rebuilt.logical_tick == 0
    assert len(persistence.scheduled_work()) == 1

    rebuilt.backend.run_until(due_at)
    flight = persistence.entity("aviation_flight", entities.leg1_id)
    assert flight is not None and flight.state == "due"


def test_restart_reference_runtime_allows_explicit_tick_without_assertion_policy():
    persistence = MemoryPersistence()
    seed_reference(persistence)
    _, engine = build_runtime(persistence)
    backend = SimPyBackend(origin=ORIGIN)
    engine.rebuild_backend(backend)

    rebuilt = restart_reference_runtime(
        persistence,
        build_runtime,
        backend,
        backend_factory=SimPyBackend,
        tick=7,
    )

    assert rebuilt.logical_tick == 7
    assert rebuilt.context.clock.tick == 7


def test_restart_reference_runtime_can_use_persisted_position_without_live_backend():
    persistence = MemoryPersistence()
    seed_reference(persistence)
    _, engine = build_runtime(persistence)
    backend = SimPyBackend(origin=ORIGIN)
    engine.rebuild_backend(backend)

    engine.advance_tick()
    backend.run_until(engine.context.clock.now)
    position = persistence.simulation_position()
    assert position is not None

    rebuilt = restart_reference_runtime(
        persistence,
        build_runtime,
        backend_factory=SimPyBackend,
    )

    assert rebuilt.restarted_at == position.logical_time
    assert rebuilt.logical_tick == position.logical_tick
    assert rebuilt.context.clock.now == position.logical_time
    assert rebuilt.context.clock.tick == position.logical_tick

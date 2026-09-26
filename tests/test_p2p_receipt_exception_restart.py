from datetime import timedelta

from sose.backends.simpy import SimPyBackend
from sose.examples.p2p.simulation import (
    ORIGIN,
    build_runtime,
    reconcile_receiving_resources,
    reconcile_stocking,
    seed_happy_path,
)
from sose.persistence.memory import MemoryPersistence


def test_partial_receipt_restart_preserves_exception_trace_and_quantity():
    persistence = MemoryPersistence()
    ids = seed_happy_path(persistence, quantity=5.0)
    _, engine = build_runtime(persistence)
    backend = SimPyBackend(origin=ORIGIN)
    engine.rebuild_backend(backend)
    backend.run_until(ORIGIN + timedelta(hours=10))

    assert reconcile_receiving_resources(
        persistence,
        engine,
        backend,
        entities=ids,
        outcome="partial",
    ) is True

    position = persistence.simulation_position()
    now = position.logical_time if position is not None else backend.now
    tick = position.logical_tick if position is not None else 0
    _, restarted_engine = build_runtime(persistence, now=now, tick=tick)
    restarted_backend = SimPyBackend(origin=now)
    restarted_engine.rebuild_backend(restarted_backend)
    restarted_backend.run_until(now)

    assert persistence.entity("receipt", ids.receipt_id).state == "inspected"
    assert any(
        event.payload["trigger"] == "mark_partial"
        for event in persistence.events()
    )

    reconcile_stocking(
        persistence,
        restarted_engine,
        restarted_backend,
        entities=ids,
        quantity=3.0,
    )

    assert persistence.entity("receipt", ids.receipt_id).state == "stocked"
    assert {
        state.name: state.level for state in persistence.container_states()
    }["inventory"] == 3.0


def test_rejected_receipt_restart_remains_terminal_without_inventory():
    persistence = MemoryPersistence()
    ids = seed_happy_path(persistence, quantity=5.0)
    _, engine = build_runtime(persistence)
    backend = SimPyBackend(origin=ORIGIN)
    engine.rebuild_backend(backend)
    backend.run_until(ORIGIN + timedelta(hours=10))

    assert reconcile_receiving_resources(
        persistence,
        engine,
        backend,
        entities=ids,
        outcome="rejected",
    ) is True

    position = persistence.simulation_position()
    now = position.logical_time if position is not None else backend.now
    tick = position.logical_tick if position is not None else 0
    _, restarted_engine = build_runtime(persistence, now=now, tick=tick)
    restarted_backend = SimPyBackend(origin=now)
    restarted_engine.rebuild_backend(restarted_backend)
    restarted_backend.run_until(now)

    assert persistence.entity("receipt", ids.receipt_id).state == "rejected"
    assert persistence.store_items() == ()
    assert persistence.container_operation_results() == ()

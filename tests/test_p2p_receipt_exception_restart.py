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


def _prepare_partial():
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
    return persistence, ids, engine, backend


def _snapshot(persistence, ids):
    return {
        "receipt": persistence.entity("receipt", ids.receipt_id),
        "demand": persistence.entity("material_demand", ids.material_demand_id),
        "events": persistence.events(),
        "store_items": persistence.store_items(),
        "store_results": persistence.store_get_results(),
        "container_states": persistence.container_states(),
        "container_results": persistence.container_operation_results(),
        "resource_demands": persistence.resource_demands(),
        "resource_reservations": persistence.resource_reservations(),
    }


def test_partial_receipt_restart_is_semantically_equivalent():
    continuous, continuous_ids, continuous_engine, continuous_backend = (
        _prepare_partial()
    )
    reconcile_stocking(
        continuous,
        continuous_engine,
        continuous_backend,
        entities=continuous_ids,
        quantity=3.0,
    )

    restarted, restarted_ids, _, backend = _prepare_partial()
    position = restarted.simulation_position()
    now = position.logical_time if position is not None else backend.now
    tick = position.logical_tick if position is not None else 0
    _, restarted_engine = build_runtime(restarted, now=now, tick=tick)
    restarted_backend = SimPyBackend(origin=now)
    restarted_engine.rebuild_backend(restarted_backend)
    restarted_backend.run_until(now)

    assert restarted.entity("receipt", restarted_ids.receipt_id).state == "inspected"
    assert any(
        event.payload["trigger"] == "mark_partial"
        for event in restarted.events()
    )

    reconcile_stocking(
        restarted,
        restarted_engine,
        restarted_backend,
        entities=restarted_ids,
        quantity=3.0,
    )

    assert _snapshot(restarted, restarted_ids) == _snapshot(
        continuous, continuous_ids
    )
    assert {
        state.name: state.level for state in restarted.container_states()
    }["inventory"] == 3.0


def test_rejected_receipt_restart_preserves_terminal_inventory_neutral_state():
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
    before = _snapshot(persistence, ids)

    position = persistence.simulation_position()
    now = position.logical_time if position is not None else backend.now
    tick = position.logical_tick if position is not None else 0
    _, restarted_engine = build_runtime(persistence, now=now, tick=tick)
    restarted_backend = SimPyBackend(origin=now)
    restarted_engine.rebuild_backend(restarted_backend)
    restarted_backend.run_until(now)

    assert _snapshot(persistence, ids) == before
    assert persistence.entity("receipt", ids.receipt_id).state == "rejected"
    assert persistence.store_items() == ()
    assert persistence.container_operation_results() == ()

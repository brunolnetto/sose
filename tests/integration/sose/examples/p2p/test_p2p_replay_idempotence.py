from __future__ import annotations

from sose.backends.simpy import SimPyBackend
from sose.examples.p2p.simulation import (
    build_runtime,
    reconcile_consumption,
    reconcile_stocking,
    run_happy_path,
)


def _durable_snapshot(persistence, ids):
    return {
        "requisition": persistence.entity("requisition", ids.requisition_id),
        "purchase_order": persistence.entity("purchase_order", ids.purchase_order_id),
        "receipt": persistence.entity("receipt", ids.receipt_id),
        "demand": persistence.entity("material_demand", ids.material_demand_id),
        "events": persistence.events(),
        "scheduled_work": persistence.scheduled_work(),
        "store_items": persistence.store_items(),
        "store_put_intents": persistence.store_put_intents(),
        "store_get_requests": persistence.store_get_requests(),
        "store_get_results": persistence.store_get_results(),
        "container_states": persistence.container_states(),
        "container_intents": persistence.container_operation_intents(),
        "container_results": persistence.container_operation_results(),
    }


def test_terminal_stocking_and_consumption_replay_is_idempotent() -> None:
    persistence, ids = run_happy_path(quantity=5.0)
    before = _durable_snapshot(persistence, ids)

    position = persistence.simulation_position()
    assert position is not None
    _, engine = build_runtime(
        persistence,
        now=position.logical_time,
        tick=position.logical_tick,
    )
    backend = SimPyBackend(origin=position.logical_time)
    engine.rebuild_backend(backend)
    backend.run_until(backend.now)

    reconcile_stocking(
        persistence,
        engine,
        backend,
        entities=ids,
        quantity=5.0,
    )
    reconcile_consumption(
        persistence,
        engine,
        backend,
        entities=ids,
        quantity=5.0,
    )

    assert _durable_snapshot(persistence, ids) == before

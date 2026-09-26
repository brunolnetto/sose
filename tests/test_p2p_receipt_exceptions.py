from datetime import timedelta

import pytest

from sose.backends.simpy import SimPyBackend
from sose.examples.p2p.simulation import (
    ORIGIN,
    build_runtime,
    flow_correlation_id,
    reconcile_consumption,
    reconcile_receiving_resources,
    reconcile_stocking,
    seed_happy_path,
)
from sose.persistence.memory import MemoryPersistence


def _receipt_runtime(quantity=5.0):
    persistence = MemoryPersistence()
    ids = seed_happy_path(persistence, quantity=quantity)
    context, engine = build_runtime(persistence)
    backend = SimPyBackend(origin=ORIGIN)
    engine.rebuild_backend(backend)
    backend.run_until(ORIGIN + timedelta(hours=10))
    return persistence, ids, context, engine, backend


def test_partial_receipt_stocks_only_received_quantity_and_leaves_shortage_explicit():
    persistence, ids, context, engine, backend = _receipt_runtime(quantity=5.0)

    assert reconcile_receiving_resources(
        persistence,
        engine,
        backend,
        entities=ids,
        outcome="partial",
    ) is True
    assert persistence.entity("receipt", ids.receipt_id).state == "inspected"

    reconcile_stocking(
        persistence,
        engine,
        backend,
        entities=ids,
        quantity=3.0,
    )
    assert persistence.entity("receipt", ids.receipt_id).state == "stocked"
    assert {
        state.name: state.level for state in persistence.container_states()
    }["inventory"] == 3.0

    with pytest.raises(RuntimeError, match="inventory withdrawal is still pending"):
        reconcile_consumption(
            persistence,
            engine,
            backend,
            entities=ids,
            quantity=5.0,
        )

    demand = persistence.entity("material_demand", ids.material_demand_id)
    for trigger in ("wait_for_inventory", "backorder"):
        command = context.commands.create(
            trigger,
            target=demand,
            correlation_id=flow_correlation_id(),
            key=("p2p-partial", demand.id, trigger),
        )
        engine.dispatch(command)
        demand = persistence.entity("material_demand", ids.material_demand_id)

    assert demand.state == "backordered"
    assert [intent.request_id for intent in persistence.container_operation_intents()] == [
        "consume-demand-1"
    ]
    assert [request.request_id for request in persistence.store_get_requests()] == [
        "consume-receipt-lot-1"
    ]
    triggers = [e.payload["trigger"] for e in persistence.events()]
    assert "mark_partial" in triggers


def test_rejected_receipt_never_creates_inventory():
    persistence, ids, _, engine, backend = _receipt_runtime(quantity=5.0)

    assert reconcile_receiving_resources(
        persistence,
        engine,
        backend,
        entities=ids,
        outcome="rejected",
    ) is True

    assert persistence.entity("receipt", ids.receipt_id).state == "rejected"
    assert persistence.store_items() == ()
    assert persistence.store_put_intents() == ()
    assert persistence.container_operation_results() == ()
    assert {
        state.name: state.level for state in persistence.container_states()
    }["inventory"] == 0.0
    assert persistence.resource_reservations() == ()
    assert persistence.resource_demands() == ()


def test_receipt_outcome_validation_is_explicit():
    persistence, ids, _, engine, backend = _receipt_runtime(quantity=5.0)

    with pytest.raises(ValueError, match="unsupported receipt outcome"):
        reconcile_receiving_resources(
            persistence,
            engine,
            backend,
            entities=ids,
            outcome="damaged-but-unknown",
        )

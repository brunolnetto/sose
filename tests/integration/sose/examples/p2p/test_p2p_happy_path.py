from datetime import timedelta

import pytest

from sose.backends.simpy import SimPyBackend
from sose.examples.p2p.simulation import (
    INVENTORY_CAPACITY,
    ORIGIN,
    build_runtime,
    flow_correlation_id,
    reconcile_consumption,
    reconcile_receiving_resources,
    reconcile_stocking,
    run_happy_path,
    seed_happy_path,
)
from sose.persistence.memory import MemoryPersistence


def test_p2p_happy_path_reaches_consumed_inventory():
    persistence, ids = run_happy_path(quantity=10.0)

    assert persistence.entity("requisition", ids.requisition_id).state == "ordered"
    assert persistence.entity("purchase_order", ids.purchase_order_id).state == "closed"
    assert persistence.entity("receipt", ids.receipt_id).state == "stocked"
    assert persistence.entity("material_demand", ids.material_demand_id).state == "consumed"

    assert persistence.store_items() == ()
    assert [result.request_id for result in persistence.store_get_results()] == [
        "consume-receipt-lot-1"
    ]
    assert {state.name: state.level for state in persistence.container_states()} == {
        "inventory": 0.0
    }
    assert [
        result.request_id for result in persistence.container_operation_results()
    ] == ["stock-receipt-1", "consume-demand-1"]
    assert persistence.scheduled_work() == ()

    events = persistence.events()
    assert [event.payload["trigger"] for event in events] == [
        "approve",
        "order",
        "submit",
        "confirm",
        "dispatch",
        "receive",
        "begin_receiving",
        "inspect",
        "close",
        "stock",
        "allocate",
        "consume",
    ]
    assert {event.correlation_id for event in events} == {flow_correlation_id()}


def test_supplier_lead_time_is_durable_scheduled_work():
    persistence = MemoryPersistence()
    ids = seed_happy_path(persistence, quantity=5.0)

    purchase_order_work = [
        work
        for work in persistence.scheduled_work()
        if persistence.command(work.command_id).entity_id == ids.purchase_order_id
    ]

    receive = next(
        persistence.command(work.command_id)
        for work in purchase_order_work
        if persistence.command(work.command_id).name == "receive"
    )
    dispatch = next(
        persistence.command(work.command_id)
        for work in purchase_order_work
        if persistence.command(work.command_id).name == "dispatch"
    )

    assert dispatch.due_at == ORIGIN + timedelta(hours=4)
    assert receive.due_at == ORIGIN + timedelta(hours=10)
    assert receive.due_at - dispatch.due_at == timedelta(hours=6)
    assert {
        persistence.command(work.command_id).correlation_id
        for work in persistence.scheduled_work()
    } == {flow_correlation_id()}


def test_stocking_reconciliation_recovers_from_partial_inventory_commit():
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
    ) is True
    backend.run_until(ORIGIN + timedelta(hours=11))

    engine.stores.put(
        backend,
        store_name="received_lots",
        item_id="receipt-lot-1",
        value={"sku": "bearing-6204", "quantity": 5.0},
        requested_at=backend.now,
    )
    backend.run_until(backend.now)

    assert [item.item_id for item in persistence.store_items()] == ["receipt-lot-1"]
    assert persistence.container_operation_results() == ()
    assert persistence.entity("receipt", ids.receipt_id).state == "inspected"

    restarted_context, restarted_engine = build_runtime(
        persistence,
        now=backend.now,
    )
    restarted_backend = SimPyBackend(origin=backend.now)
    restarted_engine.rebuild_backend(restarted_backend)
    restarted_backend.run_until(restarted_backend.now)

    reconcile_stocking(
        persistence,
        restarted_engine,
        restarted_backend,
        entities=ids,
        quantity=5.0,
    )
    reconcile_consumption(
        persistence,
        restarted_engine,
        restarted_backend,
        entities=ids,
        quantity=5.0,
    )

    assert persistence.entity("receipt", ids.receipt_id).state == "stocked"
    assert persistence.entity("material_demand", ids.material_demand_id).state == "consumed"
    assert restarted_context.clock.now == backend.now


def test_quantity_must_fit_inventory_capacity():
    with pytest.raises(ValueError, match="inventory capacity"):
        run_happy_path(quantity=INVENTORY_CAPACITY + 1.0)


def test_illegal_receipt_stock_transition_is_a_no_op():
    persistence = MemoryPersistence()
    ids = seed_happy_path(persistence)
    context, engine = build_runtime(persistence)
    receipt = persistence.entity("receipt", ids.receipt_id)

    command = context.commands.create(
        "stock",
        target=receipt,
        key=("illegal-stock", receipt.id),
    )

    engine.dispatch(command)

    assert persistence.entity("receipt", ids.receipt_id).state == "pending"
    assert persistence.events() == ()

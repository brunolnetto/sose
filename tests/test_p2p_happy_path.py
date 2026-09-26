from datetime import timedelta

from sose.examples.p2p.simulation import ORIGIN, build_runtime, run_happy_path, seed_happy_path
from sose.persistence.memory import MemoryPersistence


def test_p2p_happy_path_reaches_stocked_inventory():
    persistence, ids = run_happy_path(quantity=10.0)

    assert persistence.entity("requisition", ids.requisition_id).state == "ordered"
    assert persistence.entity("purchase_order", ids.purchase_order_id).state == "closed"
    assert persistence.entity("receipt", ids.receipt_id).state == "stocked"
    assert persistence.entity("material_demand", ids.material_demand_id).state == "open"

    assert [item.item_id for item in persistence.store_items()] == ["receipt-lot-1"]
    assert {state.name: state.level for state in persistence.container_states()} == {
        "inventory": 10.0
    }
    assert persistence.scheduled_work() == ()

    triggers = [event.payload["trigger"] for event in persistence.events()]
    assert triggers == [
        "approve",
        "order",
        "submit",
        "confirm",
        "dispatch",
        "receive",
        "begin_receiving",
        "close",
        "inspect",
        "stock",
    ]


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


def test_illegal_receipt_stock_transition_is_rejected():
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

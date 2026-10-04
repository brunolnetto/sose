from sose.examples.p2p.simulation import run_shortage_backorder


def test_shortage_waits_durably_then_consumes_replenishment_exactly_once():
    persistence, ids = run_shortage_backorder(quantity=5.0)

    demand = persistence.entity("material_demand", ids.material_demand_id)
    assert demand is not None
    assert demand.state == "consumed"

    assert persistence.store_get_requests() == ()
    assert persistence.store_put_intents() == ()
    assert persistence.store_items() == ()
    assert [result.request_id for result in persistence.store_get_results()] == [
        "lot-for-demand-1"
    ]
    assert persistence.store_get_results()[0].item.item_id == "replenishment-lot-1"

    assert persistence.container_operation_intents() == ()
    assert {state.name: state.level for state in persistence.container_states()} == {
        "inventory": 0.0
    }
    assert [result.request_id for result in persistence.container_operation_results()] == [
        "quantity-for-demand-1",
        "replenish-demand-1",
    ]

    demand_triggers = [
        event.payload["trigger"]
        for event in persistence.events()
        if event.entity_type == "material_demand"
    ]
    assert demand_triggers == [
        "wait_for_inventory",
        "backorder",
        "allocate",
        "consume",
    ]


def test_backorder_flow_leaves_no_hidden_inventory_after_consumption():
    persistence, _ = run_shortage_backorder(quantity=7.0)

    state = next(
        state for state in persistence.container_states() if state.name == "inventory"
    )
    assert state.level == 0.0
    assert persistence.store_items() == ()

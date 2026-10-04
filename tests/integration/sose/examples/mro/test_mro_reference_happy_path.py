from sose.examples.mro.simulation import flow_correlation_id, run_happy_path


def test_mro_happy_path_reaches_closed_with_consumed_part_and_released_capacity():
    persistence, ids = run_happy_path(quantity=1.0)

    assert persistence.entity("work_order", ids.work_order_id).state == "closed"
    assert persistence.entity("part_demand", ids.part_demand_id).state == "consumed"
    assert persistence.store_items() == ()
    assert {state.name: state.level for state in persistence.container_states()} == {
        "spare_parts": 0.0
    }
    assert persistence.resource_reservations() == ()
    assert persistence.preemptive_resource_reservations() == ()
    assert {event.correlation_id for event in persistence.events()} == {
        flow_correlation_id()
    }


def test_mro_happy_path_commits_part_issue_before_in_progress():
    persistence, ids = run_happy_path(quantity=2.0)
    events = persistence.events()
    start_event = next(
        event for event in events if event.payload["trigger"] == "start"
    )
    lot_result = next(
        result
        for result in persistence.store_get_results()
        if result.request_id == "consume-part-lot-1"
    )
    quantity_result = next(
        result
        for result in persistence.container_operation_results()
        if result.request_id == "consume-spare-part-1"
    )
    assert lot_result.completed_at <= start_event.occurred_at
    assert quantity_result.completed_at <= start_event.occurred_at

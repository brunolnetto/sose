from sose.examples.logistics.entities import DeliveryAttempt, Shipment
from sose.examples.logistics.statecharts import DeliveryAttemptChart, ShipmentChart
from sose.statecharts.topology import graph_from_statechart, policy_from_statechart


def test_logistics_entities_have_stable_types():
    assert Shipment(id="shipment-1").entity_type == "shipment"
    assert DeliveryAttempt(id="attempt-1").entity_type == "delivery_attempt"


def test_shipment_chart_covers_nominal_flow_and_representative_exceptions():
    graph = graph_from_statechart(ShipmentChart())
    edges = {(edge.source, edge.event, edge.targets) for edge in graph.edges}

    assert ("created", "schedule_pickup", ("pickup_scheduled",)) in edges
    assert ("pickup_scheduled", "pickup", ("picked_up",)) in edges
    assert ("at_origin_hub", "dispatch_transfer", ("in_transfer",)) in edges
    assert ("in_transfer", "arrive_destination_hub", ("at_destination_hub",)) in edges
    assert ("at_destination_hub", "dispatch_delivery", ("out_for_delivery",)) in edges
    assert ("out_for_delivery", "deliver", ("delivered",)) in edges
    assert ("out_for_delivery", "mark_lost", ("lost",)) in edges
    assert ("out_for_delivery", "mark_damaged", ("damaged",)) in edges
    assert ("out_for_delivery", "return_to_sender", ("returned",)) in edges


def test_delay_preserves_the_exact_pre_delay_phase():
    graph = graph_from_statechart(ShipmentChart())
    edges = {(edge.source, edge.event, edge.targets) for edge in graph.edges}

    expected = {
        ("pickup_scheduled", "delay", ("delayed_pickup",)),
        ("delayed_pickup", "resume", ("pickup_scheduled",)),
        ("picked_up", "delay", ("delayed_after_pickup",)),
        ("delayed_after_pickup", "resume", ("picked_up",)),
        ("at_origin_hub", "delay", ("delayed_origin_hub",)),
        ("delayed_origin_hub", "resume", ("at_origin_hub",)),
        ("in_transfer", "delay", ("delayed_transfer",)),
        ("delayed_transfer", "resume", ("in_transfer",)),
        ("at_destination_hub", "delay", ("delayed_destination_hub",)),
        ("delayed_destination_hub", "resume", ("at_destination_hub",)),
        ("out_for_delivery", "delay", ("delayed_delivery",)),
        ("delayed_delivery", "resume", ("out_for_delivery",)),
    }
    assert expected <= edges


def test_delivery_attempt_failure_is_terminal_and_retry_requires_new_identity():
    chart = DeliveryAttemptChart()
    graph = graph_from_statechart(chart)
    policy = policy_from_statechart(chart)

    edges = {(edge.source, edge.event, edge.targets) for edge in graph.edges}
    assert ("pending", "dispatch", ("out_for_delivery",)) in edges
    assert ("out_for_delivery", "deliver", ("delivered",)) in edges
    assert ("out_for_delivery", "fail", ("failed",)) in edges
    assert not any(source == "failed" for source, _, _ in edges)

    assert set(policy.events) == {"deliver", "fail"}
    assert policy.is_probabilistically_eligible("deliver") is True
    assert policy.is_probabilistically_eligible("fail") is True
    assert policy.is_probabilistically_eligible("dispatch") is False


def test_shipment_chart_rejects_direct_probabilistic_dispatch():
    policy = policy_from_statechart(ShipmentChart())
    assert policy.events == ()
    for event in (
        "schedule_pickup",
        "pickup",
        "arrive_origin_hub",
        "dispatch_transfer",
        "arrive_destination_hub",
        "dispatch_delivery",
        "deliver",
        "delay",
        "resume",
        "mark_lost",
        "mark_damaged",
        "return_to_sender",
    ):
        assert policy.is_probabilistically_eligible(event) is False


def test_delayed_post_pickup_phases_allow_terminal_exceptions():
    graph = graph_from_statechart(ShipmentChart())
    edges = {(edge.source, edge.event, edge.targets) for edge in graph.edges}

    for state in (
        "delayed_after_pickup",
        "delayed_origin_hub",
        "delayed_transfer",
        "delayed_destination_hub",
        "delayed_delivery",
    ):
        assert (state, "mark_lost", ("lost",)) in edges
        assert (state, "mark_damaged", ("damaged",)) in edges

    assert ("delayed_delivery", "return_to_sender", ("returned",)) in edges

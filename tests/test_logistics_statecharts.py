from sose.examples.logistics.entities import DeliveryAttempt, Shipment
from sose.examples.logistics.statecharts import DeliveryAttemptChart, ShipmentChart
from sose.statecharts.topology import graph_from_statechart, policy_from_statechart


def test_logistics_entities_have_stable_types():
    assert Shipment.entity_type == "shipment"
    assert DeliveryAttempt.entity_type == "delivery_attempt"


def test_shipment_chart_covers_nominal_flow_and_representative_exceptions():
    graph = graph_from_statechart(ShipmentChart())
    edges = {(edge.source, edge.event, edge.targets) for edge in graph.edges}

    assert ("created", "schedule_pickup", ("pickup_scheduled",)) in edges
    assert ("pickup_scheduled", "pickup", ("picked_up",)) in edges
    assert ("at_origin_hub", "dispatch_transfer", ("in_transfer",)) in edges
    assert ("in_transfer", "arrive_destination_hub", ("at_destination_hub",)) in edges
    assert ("at_destination_hub", "dispatch_delivery", ("out_for_delivery",)) in edges
    assert ("out_for_delivery", "deliver", ("delivered",)) in edges
    assert ("in_transfer", "delay", ("delayed",)) in edges
    assert ("out_for_delivery", "mark_lost", ("lost",)) in edges
    assert ("out_for_delivery", "mark_damaged", ("damaged",)) in edges
    assert ("out_for_delivery", "return_to_sender", ("returned",)) in edges


def test_delivery_attempt_probability_is_only_for_business_outcome():
    chart = DeliveryAttemptChart()
    graph = graph_from_statechart(chart)
    policy = policy_from_statechart(chart)

    edges = {(edge.source, edge.event, edge.targets) for edge in graph.edges}
    assert ("out_for_delivery", "deliver", ("delivered",)) in edges
    assert ("out_for_delivery", "fail", ("failed",)) in edges
    assert ("failed", "schedule_retry", ("retry_scheduled",)) in edges
    assert ("retry_scheduled", "retry", ("out_for_delivery",)) in edges

    assert set(policy.events) == {"deliver", "fail"}
    assert policy.is_probabilistically_eligible("deliver") is True
    assert policy.is_probabilistically_eligible("fail") is True
    assert policy.is_probabilistically_eligible("retry") is False

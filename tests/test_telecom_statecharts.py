from sose.examples.telecom.entities import (
    NetworkAlarm,
    ProductOrder,
    ServiceOrder,
    SubscriptionService,
    TroubleTicket,
    UsageRecord,
)
from sose.examples.telecom.statecharts import (
    NetworkAlarmChart,
    ProductOrderChart,
    ServiceOrderChart,
    SubscriptionServiceChart,
    TroubleTicketChart,
    UsageRecordChart,
)
from sose.statecharts.topology import graph_from_statechart, policy_from_statechart


def _edges(chart):
    graph = graph_from_statechart(chart)
    return {(edge.source, edge.event, edge.targets) for edge in graph.edges}


def test_telecom_entities_have_stable_types():
    assert ProductOrder(id="p").entity_type == "telecom_product_order"
    assert ServiceOrder(id="s").entity_type == "telecom_service_order"
    assert SubscriptionService(id="svc").entity_type == "telecom_subscription_service"
    assert UsageRecord(id="u").entity_type == "telecom_usage_record"
    assert NetworkAlarm(id="a").entity_type == "telecom_network_alarm"
    assert TroubleTicket(id="t").entity_type == "telecom_trouble_ticket"


def test_order_and_service_activation_are_separate_lifecycles():
    assert ("captured", "acknowledge", ("acknowledged",)) in _edges(ProductOrderChart())
    assert ("acknowledged", "start", ("in_progress",)) in _edges(ProductOrderChart())
    assert ("pending", "accept", ("accepted",)) in _edges(ServiceOrderChart())
    assert ("accepted", "start_provisioning", ("provisioning",)) in _edges(ServiceOrderChart())
    assert ("designed", "start_provisioning", ("provisioning",)) in _edges(SubscriptionServiceChart())
    assert ("provisioning", "make_activation_ready", ("activation_ready",)) in _edges(SubscriptionServiceChart())
    assert ("activation_ready", "activate", ("active",)) in _edges(SubscriptionServiceChart())


def test_assurance_and_usage_are_independent_lifecycles():
    assert ("captured", "commit", ("committed",)) in _edges(UsageRecordChart())
    assert ("raised", "acknowledge", ("acknowledged",)) in _edges(NetworkAlarmChart())
    assert ("acknowledged", "clear", ("cleared",)) in _edges(NetworkAlarmChart())
    assert ("open", "acknowledge", ("acknowledged",)) in _edges(TroubleTicketChart())
    assert ("acknowledged", "resolve", ("resolved",)) in _edges(TroubleTicketChart())
    assert ("resolved", "close", ("closed",)) in _edges(TroubleTicketChart())
    assert ("active", "suspend", ("suspended",)) in _edges(SubscriptionServiceChart())
    assert ("suspended", "restore", ("active",)) in _edges(SubscriptionServiceChart())


def test_telecom_reference_transitions_are_orchestration_gated():
    for chart in (
        ProductOrderChart(),
        ServiceOrderChart(),
        SubscriptionServiceChart(),
        UsageRecordChart(),
        NetworkAlarmChart(),
        TroubleTicketChart(),
    ):
        assert policy_from_statechart(chart).events == ()

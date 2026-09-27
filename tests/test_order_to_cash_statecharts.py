from sose.examples.order_to_cash.entities import CollectionCase, Receivable, SalesOrder
from sose.examples.order_to_cash.statecharts import (
    CollectionCaseChart,
    ReceivableChart,
    SalesOrderChart,
)
from sose.statecharts.topology import graph_from_statechart, policy_from_statechart


def test_o2c_entities_have_stable_types():
    assert SalesOrder(id="order-1").entity_type == "sales_order"
    assert Receivable(id="receivable-1").entity_type == "receivable"
    assert CollectionCase(id="case-1").entity_type == "collection_case"


def test_sales_order_separates_credit_and_partial_fulfillment():
    graph = graph_from_statechart(SalesOrderChart())
    edges = {(edge.source, edge.event, edge.targets) for edge in graph.edges}

    assert ("submitted", "hold_credit", ("credit_hold",)) in edges
    assert ("credit_hold", "release_credit", ("ordered",)) in edges
    assert ("ordered", "start_fulfillment", ("fulfilling",)) in edges
    assert ("fulfilling", "record_partial", ("partial_fulfillment",)) in edges
    assert ("partial_fulfillment", "fulfill", ("fulfilled",)) in edges
    assert ("fulfilled", "ship", ("shipped",)) in edges
    assert ("shipped", "invoice", ("invoiced",)) in edges


def test_receivable_keeps_overdue_and_dispute_distinct():
    graph = graph_from_statechart(ReceivableChart())
    edges = {(edge.source, edge.event, edge.targets) for edge in graph.edges}

    assert ("open", "mark_due", ("due",)) in edges
    assert ("due", "mark_overdue", ("overdue",)) in edges
    assert ("overdue", "dispute", ("disputed",)) in edges
    assert ("disputed", "resolve_dispute", ("due",)) in edges
    assert ("overdue", "collect", ("collected",)) in edges


def test_collection_case_is_independent_occurrence():
    graph = graph_from_statechart(CollectionCaseChart())
    edges = {(edge.source, edge.event, edge.targets) for edge in graph.edges}

    assert ("opened", "assign", ("assigned",)) in edges
    assert ("assigned", "contact", ("contacted",)) in edges
    assert ("contacted", "promise", ("promised",)) in edges
    assert ("promised", "escalate", ("escalated",)) in edges
    assert ("escalated", "resolve", ("resolved",)) in edges


def test_o2c_lifecycles_are_orchestration_gated():
    for chart in (SalesOrderChart(), ReceivableChart(), CollectionCaseChart()):
        policy = policy_from_statechart(chart)
        assert policy.events == ()
        for edge in graph_from_statechart(chart).edges:
            if edge.event is not None:
                assert policy.is_probabilistically_eligible(edge.event) is False

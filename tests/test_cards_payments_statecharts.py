from sose.examples.cards_payments.entities import Dispute, Payment
from sose.examples.cards_payments.statecharts import DisputeChart, PaymentChart
from sose.statecharts.topology import graph_from_statechart, policy_from_statechart


def test_cards_entities_have_stable_types():
    assert Payment(id="payment-1").entity_type == "card_payment"
    assert Dispute(id="dispute-1").entity_type == "payment_dispute"


def test_payment_chart_separates_reversal_refund_and_settlement():
    graph = graph_from_statechart(PaymentChart())
    edges = {(edge.source, edge.event, edge.targets) for edge in graph.edges}

    assert ("authorization_requested", "authorize", ("authorized",)) in edges
    assert ("authorization_requested", "decline", ("declined",)) in edges
    assert ("authorized", "capture", ("captured",)) in edges
    assert ("authorized", "reverse", ("reversed",)) in edges
    assert ("captured", "settle", ("settled",)) in edges
    assert ("settled", "refund", ("refunded",)) in edges
    assert ("captured", "reverse", ("reversed",)) not in edges
    assert ("authorized", "refund", ("refunded",)) not in edges


def test_only_authorization_decision_is_directly_probabilistic():
    policy = policy_from_statechart(PaymentChart())

    assert set(policy.events) == {"authorize", "decline"}
    assert policy.is_probabilistically_eligible("authorize") is True
    assert policy.is_probabilistically_eligible("decline") is True
    assert policy.is_probabilistically_eligible("capture") is False
    assert policy.is_probabilistically_eligible("reverse") is False
    assert policy.is_probabilistically_eligible("settle") is False
    assert policy.is_probabilistically_eligible("refund") is False


def test_dispute_is_an_independent_post_settlement_lifecycle():
    graph = graph_from_statechart(DisputeChart())
    edges = {(edge.source, edge.event, edge.targets) for edge in graph.edges}

    assert ("opened", "request_evidence", ("evidence_requested",)) in edges
    assert ("evidence_requested", "submit_evidence", ("under_review",)) in edges
    assert ("under_review", "issue_chargeback", ("chargeback",)) in edges
    assert ("chargeback", "resolve_merchant", ("merchant_won",)) in edges
    assert ("chargeback", "resolve_cardholder", ("cardholder_won",)) in edges

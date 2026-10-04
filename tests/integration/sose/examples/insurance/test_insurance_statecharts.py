from sose.examples.insurance.entities import (
    Assessment,
    Claim,
    DocumentRequest,
    FraudInvestigation,
    Payment,
    Policy,
    Reserve,
)
from sose.examples.insurance.statecharts import (
    AssessmentChart,
    ClaimChart,
    DocumentRequestChart,
    FraudInvestigationChart,
    PaymentChart,
    PolicyChart,
    ReserveChart,
)
from sose.statecharts.topology import graph_from_statechart, policy_from_statechart


def test_insurance_entities_have_stable_types():
    assert Policy(id="p").entity_type == "insurance_policy"
    assert Claim(id="c").entity_type == "insurance_claim"
    assert DocumentRequest(id="d").entity_type == "insurance_document_request"
    assert Assessment(id="a").entity_type == "insurance_assessment"
    assert Reserve(id="r").entity_type == "insurance_reserve"
    assert Payment(id="pay").entity_type == "insurance_payment"
    assert FraudInvestigation(id="f").entity_type == "insurance_fraud_investigation"


def test_claim_topology_separates_documents_fraud_and_payment():
    graph = graph_from_statechart(ClaimChart())
    edges = {(e.source, e.event, e.targets) for e in graph.edges}
    assert ("opened", "request_documents", ("pending_documents",)) in edges
    assert ("pending_documents", "documents_ready", ("ready_for_assessment",)) in edges
    assert ("assessing", "flag_fraud", ("fraud_review",)) in edges
    assert ("fraud_review", "clear_fraud", ("ready_for_assessment",)) in edges
    assert ("assessing", "approve", ("approved",)) in edges
    assert ("approved", "schedule_payment", ("payment_scheduled",)) in edges
    assert ("payment_scheduled", "record_payment", ("paid",)) in edges
    assert ("rejected", "reopen", ("reopened",)) in edges
    assert ("reopened", "request_documents", ("pending_documents",)) in edges


def test_payment_date_is_distinct_from_payout_execution():
    graph = graph_from_statechart(PaymentChart())
    edges = {(e.source, e.event, e.targets) for e in graph.edges}
    assert ("planned", "schedule", ("scheduled",)) in edges
    assert ("scheduled", "make_due", ("due",)) in edges
    assert ("due", "record_partial", ("partially_paid",)) in edges
    assert ("due", "complete", ("paid",)) in edges
    assert ("partially_paid", "complete", ("paid",)) in edges


def test_insurance_statecharts_are_orchestration_gated():
    for chart in (
        PolicyChart(),
        ClaimChart(),
        DocumentRequestChart(),
        AssessmentChart(),
        ReserveChart(),
        PaymentChart(),
        FraudInvestigationChart(),
    ):
        policy = policy_from_statechart(chart)
        assert policy.events == ()
        for edge in graph_from_statechart(chart).edges:
            if edge.event is not None:
                assert policy.is_probabilistically_eligible(edge.event) is False

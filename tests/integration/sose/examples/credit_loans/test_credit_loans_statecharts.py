from sose.examples.credit_loans.entities import (
    CollectionCase,
    CreditDecision,
    DelinquencyCase,
    Installment,
    Loan,
    LoanApplication,
    Payment,
    Restructure,
)
from sose.examples.credit_loans.statecharts import (
    CollectionCaseChart,
    CreditDecisionChart,
    DelinquencyCaseChart,
    InstallmentChart,
    LoanApplicationChart,
    LoanChart,
    PaymentChart,
    RestructureChart,
)
from sose.statecharts.topology import graph_from_statechart, policy_from_statechart


def test_credit_loan_entities_have_stable_types():
    assert LoanApplication(id="a").entity_type == "loan_application"
    assert CreditDecision(id="d").entity_type == "credit_decision"
    assert Loan(id="l").entity_type == "loan"
    assert Installment(id="i").entity_type == "loan_installment"
    assert Payment(id="p").entity_type == "loan_payment"
    assert DelinquencyCase(id="dc").entity_type == "delinquency_case"
    assert CollectionCase(id="c").entity_type == "loan_collection_case"
    assert Restructure(id="r").entity_type == "loan_restructure"


def test_application_exposes_only_approval_outcome_as_probabilistic_choice():
    policy = policy_from_statechart(LoanApplicationChart())
    assert policy.is_probabilistically_eligible("approve") is True
    assert policy.is_probabilistically_eligible("reject") is True
    assert policy.is_probabilistically_eligible("start_analysis") is False


def test_installment_exposes_miss_but_not_ledger_transitions_probabilistically():
    policy = policy_from_statechart(InstallmentChart())
    assert policy.is_probabilistically_eligible("miss") is True
    for event in ("make_due", "record_partial", "complete", "restructure"):
        assert policy.is_probabilistically_eligible(event) is False


def test_lending_topology_keeps_financial_occurrences_separate():
    loan_edges = {(e.source, e.event, e.targets) for e in graph_from_statechart(LoanChart()).edges}
    assert ("servicing", "mark_delinquent", ("delinquent",)) in loan_edges
    assert ("delinquent", "begin_restructure", ("restructuring",)) in loan_edges
    assert ("restructuring", "resume_servicing", ("servicing",)) in loan_edges

    installment_edges = {
        (e.source, e.event, e.targets)
        for e in graph_from_statechart(InstallmentChart()).edges
    }
    assert ("due", "record_partial", ("partially_paid",)) in installment_edges
    assert ("partially_paid", "miss", ("overdue",)) in installment_edges
    assert ("overdue", "restructure", ("restructured",)) in installment_edges

    for chart in (
        CreditDecisionChart(),
        PaymentChart(),
        DelinquencyCaseChart(),
        CollectionCaseChart(),
        RestructureChart(),
    ):
        policy = policy_from_statechart(chart)
        assert policy.events == ()

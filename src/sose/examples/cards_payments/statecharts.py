from statemachine import State, StateChart

from sose.probability import probabilistic_transitions


@probabilistic_transitions(
    {"authorize": 0.95, "decline": 0.05},
    excluded_events={
        "capture",
        "reverse",
        "settlement_due",
        "settle",
        "wait_retry",
        "retry_settlement",
        "refund",
    },
)
class PaymentChart(StateChart):
    authorization_requested = State(initial=True)
    authorized = State()
    captured = State()
    settlement_pending = State()
    settlement_retry_wait = State()
    settled = State()
    declined = State(final=True)
    reversed = State(final=True)
    refunded = State(final=True)

    authorize = authorization_requested.to(authorized)
    decline = authorization_requested.to(declined)
    capture = authorized.to(captured)
    reverse = authorized.to(reversed)
    settlement_due = captured.to(settlement_pending)
    settle = settlement_pending.to(settled)
    wait_retry = settlement_pending.to(settlement_retry_wait)
    retry_settlement = settlement_retry_wait.to(settlement_pending)
    refund = settled.to(refunded)


@probabilistic_transitions(
    {},
    excluded_events={
        "request_evidence",
        "submit_evidence",
        "issue_chargeback",
        "resolve_merchant",
        "resolve_cardholder",
        "withdraw",
    },
)
class DisputeChart(StateChart):
    opened = State(initial=True)
    evidence_requested = State()
    under_review = State()
    chargeback = State()
    merchant_won = State(final=True)
    cardholder_won = State(final=True)
    withdrawn = State(final=True)

    request_evidence = opened.to(evidence_requested)
    submit_evidence = evidence_requested.to(under_review)
    issue_chargeback = under_review.to(chargeback)
    resolve_merchant = chargeback.to(merchant_won)
    resolve_cardholder = chargeback.to(cardholder_won)
    withdraw = (
        opened.to(withdrawn)
        | evidence_requested.to(withdrawn)
        | under_review.to(withdrawn)
    )

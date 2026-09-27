from statemachine import State, StateChart

from sose.probability import probabilistic_transitions


@probabilistic_transitions(
    {"authorize": 0.95, "decline": 0.05},
    excluded_events={"capture", "reverse", "settle", "refund"},
)
class PaymentChart(StateChart):
    authorization_requested = State(initial=True)
    authorized = State()
    captured = State()
    settled = State()
    declined = State(final=True)
    reversed = State(final=True)
    refunded = State(final=True)

    authorize = authorization_requested.to(authorized)
    decline = authorization_requested.to(declined)
    capture = authorized.to(captured)
    reverse = authorized.to(reversed)
    settle = captured.to(settled)
    refund = settled.to(refunded)


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

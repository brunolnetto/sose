from statemachine import State, StateChart

from sose.probability import probabilistic_transitions


@probabilistic_transitions(
    {},
    excluded_events={
        "approve_credit",
        "hold_credit",
        "release_credit",
        "start_fulfillment",
        "record_partial",
        "fulfill",
        "ship",
        "invoice",
        "cancel",
    },
)
class SalesOrderChart(StateChart):
    submitted = State(initial=True)
    credit_hold = State()
    ordered = State()
    fulfilling = State()
    partial_fulfillment = State()
    fulfilled = State()
    shipped = State()
    invoiced = State(final=True)
    cancelled = State(final=True)

    approve_credit = submitted.to(ordered)
    hold_credit = submitted.to(credit_hold)
    release_credit = credit_hold.to(ordered)
    start_fulfillment = ordered.to(fulfilling)
    record_partial = fulfilling.to(partial_fulfillment)
    fulfill = fulfilling.to(fulfilled) | partial_fulfillment.to(fulfilled)
    ship = fulfilled.to(shipped)
    invoice = shipped.to(invoiced)
    cancel = (
        submitted.to(cancelled)
        | credit_hold.to(cancelled)
        | ordered.to(cancelled)
    )


@probabilistic_transitions(
    {},
    excluded_events={"mark_due", "collect", "mark_overdue", "dispute", "resolve_dispute"},
)
class ReceivableChart(StateChart):
    open = State(initial=True)
    due = State()
    overdue = State()
    disputed = State()
    collected = State(final=True)

    mark_due = open.to(due)
    collect = due.to(collected) | overdue.to(collected)
    mark_overdue = due.to(overdue)
    dispute = due.to(disputed) | overdue.to(disputed)
    resolve_dispute = disputed.to(due)


@probabilistic_transitions(
    {},
    excluded_events={"assign", "contact", "promise", "resolve", "escalate"},
)
class CollectionCaseChart(StateChart):
    opened = State(initial=True)
    assigned = State()
    contacted = State()
    promised = State()
    escalated = State()
    resolved = State(final=True)

    assign = opened.to(assigned)
    contact = assigned.to(contacted)
    promise = contacted.to(promised)
    resolve = contacted.to(resolved) | promised.to(resolved) | escalated.to(resolved)
    escalate = contacted.to(escalated) | promised.to(escalated)

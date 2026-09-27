from statemachine import State, StateChart

from sose.probability import probabilistic_transitions


@probabilistic_transitions(
    {},
    excluded_events={"lapse", "cancel", "reinstate"},
)
class PolicyChart(StateChart):
    active = State(initial=True)
    lapsed = State()
    cancelled = State(final=True)

    lapse = active.to(lapsed)
    reinstate = lapsed.to(active)
    cancel = active.to(cancelled) | lapsed.to(cancelled)


@probabilistic_transitions(
    {},
    excluded_events={
        "request_documents",
        "documents_ready",
        "start_assessment",
        "flag_fraud",
        "clear_fraud",
        "approve",
        "reject",
        "schedule_payment",
        "record_payment",
        "reopen",
    },
)
class ClaimChart(StateChart):
    opened = State(initial=True)
    pending_documents = State()
    ready_for_assessment = State()
    assessing = State()
    fraud_review = State()
    approved = State()
    payment_scheduled = State()
    paid = State()
    rejected = State()
    reopened = State()

    request_documents = opened.to(pending_documents)
    documents_ready = pending_documents.to(ready_for_assessment)
    start_assessment = ready_for_assessment.to(assessing) | reopened.to(assessing)
    flag_fraud = assessing.to(fraud_review)
    clear_fraud = fraud_review.to(assessing)
    approve = assessing.to(approved)
    reject = assessing.to(rejected) | fraud_review.to(rejected)
    schedule_payment = approved.to(payment_scheduled)
    record_payment = payment_scheduled.to(paid)
    reopen = paid.to(reopened) | rejected.to(reopened)


@probabilistic_transitions(
    {},
    excluded_events={"satisfy", "expire"},
)
class DocumentRequestChart(StateChart):
    open = State(initial=True)
    satisfied = State(final=True)
    expired = State(final=True)

    satisfy = open.to(satisfied)
    expire = open.to(expired)


@probabilistic_transitions(
    {},
    excluded_events={"start", "approve", "reject", "flag_fraud"},
)
class AssessmentChart(StateChart):
    pending = State(initial=True)
    in_progress = State()
    approved = State(final=True)
    rejected = State(final=True)
    fraud_flagged = State(final=True)

    start = pending.to(in_progress)
    approve = in_progress.to(approved)
    reject = in_progress.to(rejected)
    flag_fraud = in_progress.to(fraud_flagged)


@probabilistic_transitions(
    {},
    excluded_events={"establish", "release"},
)
class ReserveChart(StateChart):
    proposed = State(initial=True)
    established = State()
    released = State(final=True)

    establish = proposed.to(established)
    release = established.to(released)


@probabilistic_transitions(
    {},
    excluded_events={"schedule", "make_due", "record_partial", "complete", "fail", "retry"},
)
class PaymentChart(StateChart):
    planned = State(initial=True)
    scheduled = State()
    due = State()
    partially_paid = State()
    failed = State()
    paid = State(final=True)

    schedule = planned.to(scheduled)
    make_due = scheduled.to(due)
    record_partial = due.to(partially_paid)
    complete = due.to(paid) | partially_paid.to(paid)
    fail = due.to(failed) | partially_paid.to(failed)
    retry = failed.to(due)


@probabilistic_transitions(
    {},
    excluded_events={"assign", "clear", "confirm"},
)
class FraudInvestigationChart(StateChart):
    opened = State(initial=True)
    assigned = State()
    cleared = State(final=True)
    confirmed = State(final=True)

    assign = opened.to(assigned)
    clear = assigned.to(cleared)
    confirm = assigned.to(confirmed)

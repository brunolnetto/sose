from statemachine import State, StateChart

from sose.probability import probabilistic_transitions


@probabilistic_transitions(
    {"approve": 0.8, "reject": 0.2},
    strict=False,
    excluded_events={"start_analysis"},
)
class LoanApplicationChart(StateChart):
    submitted = State(initial=True)
    under_analysis = State()
    approved = State(final=True)
    rejected = State(final=True)

    start_analysis = submitted.to(under_analysis)
    approve = under_analysis.to(approved)
    reject = under_analysis.to(rejected)


@probabilistic_transitions(
    {},
    excluded_events={"approve", "reject"},
)
class CreditDecisionChart(StateChart):
    pending = State(initial=True)
    approved = State(final=True)
    rejected = State(final=True)

    approve = pending.to(approved)
    reject = pending.to(rejected)


@probabilistic_transitions(
    {},
    excluded_events={
        "disburse",
        "activate",
        "mark_delinquent",
        "cure",
        "begin_restructure",
        "resume_servicing",
        "pay_off",
        "default",
    },
)
class LoanChart(StateChart):
    approved = State(initial=True)
    disbursed = State()
    servicing = State()
    delinquent = State()
    restructuring = State()
    paid_off = State(final=True)
    defaulted = State(final=True)

    disburse = approved.to(disbursed)
    activate = disbursed.to(servicing)
    mark_delinquent = servicing.to(delinquent)
    cure = delinquent.to(servicing)
    begin_restructure = delinquent.to(restructuring)
    resume_servicing = restructuring.to(servicing)
    pay_off = servicing.to(paid_off) | delinquent.to(paid_off)
    default = delinquent.to(defaulted)


@probabilistic_transitions(
    {"miss": 0.15},
    strict=False,
    excluded_events={"make_due", "record_partial", "complete", "restructure"},
)
class InstallmentChart(StateChart):
    scheduled = State(initial=True)
    due = State()
    partially_paid = State()
    overdue = State()
    paid = State(final=True)
    restructured = State(final=True)

    make_due = scheduled.to(due)
    record_partial = due.to(partially_paid) | overdue.to(partially_paid)
    miss = due.to(overdue) | partially_paid.to(overdue)
    complete = (
        due.to(paid)
        | partially_paid.to(paid)
        | overdue.to(paid)
    )
    restructure = (
        scheduled.to(restructured)
        | due.to(restructured)
        | partially_paid.to(restructured)
        | overdue.to(restructured)
    )


@probabilistic_transitions(
    {},
    excluded_events={"post", "fail"},
)
class PaymentChart(StateChart):
    initiated = State(initial=True)
    posted = State(final=True)
    failed = State(final=True)

    post = initiated.to(posted)
    fail = initiated.to(failed)


@probabilistic_transitions(
    {},
    excluded_events={"begin_collection", "cure", "restructure", "default"},
)
class DelinquencyCaseChart(StateChart):
    opened = State(initial=True)
    collection = State()
    cured = State(final=True)
    restructured = State(final=True)
    defaulted = State(final=True)

    begin_collection = opened.to(collection)
    cure = opened.to(cured) | collection.to(cured)
    restructure = opened.to(restructured) | collection.to(restructured)
    default = collection.to(defaulted)


@probabilistic_transitions(
    {},
    excluded_events={"assign", "contact", "promise", "escalate", "resolve"},
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
    escalate = contacted.to(escalated) | promised.to(escalated)
    resolve = contacted.to(resolved) | promised.to(resolved) | escalated.to(resolved)


@probabilistic_transitions(
    {},
    excluded_events={"accept", "reject", "apply"},
)
class RestructureChart(StateChart):
    proposed = State(initial=True)
    accepted = State()
    rejected = State(final=True)
    applied = State(final=True)

    accept = proposed.to(accepted)
    reject = proposed.to(rejected)
    apply = accepted.to(applied)

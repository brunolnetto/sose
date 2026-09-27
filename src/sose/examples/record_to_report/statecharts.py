from statemachine import State, StateChart

from sose.probability import probabilistic_transitions


@probabilistic_transitions(
    {},
    excluded_events={"submit", "post", "reject"},
)
class JournalEntryChart(StateChart):
    drafted = State(initial=True)
    submitted = State()
    posted = State(final=True)
    rejected = State(final=True)

    submit = drafted.to(submitted)
    post = submitted.to(posted)
    reject = submitted.to(rejected)


@probabilistic_transitions(
    {},
    excluded_events={
        "start",
        "match",
        "mark_unmatched",
        "require_adjustment",
        "apply_adjustment",
        "reject",
    },
)
class ReconciliationItemChart(StateChart):
    pending = State(initial=True)
    reconciling = State()
    unmatched = State()
    adjustment_required = State()
    matched = State(final=True)
    reconciled = State(final=True)
    rejected = State(final=True)

    start = pending.to(reconciling)
    match = reconciling.to(matched)
    mark_unmatched = reconciling.to(unmatched)
    require_adjustment = unmatched.to(adjustment_required)
    apply_adjustment = adjustment_required.to(reconciled)
    reject = unmatched.to(rejected) | adjustment_required.to(rejected)


@probabilistic_transitions(
    {},
    excluded_events={"submit", "approve", "post", "reject"},
)
class AdjustmentChart(StateChart):
    proposed = State(initial=True)
    submitted = State()
    approved = State()
    posted = State(final=True)
    rejected = State(final=True)

    submit = proposed.to(submitted)
    approve = submitted.to(approved)
    post = approved.to(posted)
    reject = submitted.to(rejected) | approved.to(rejected)


@probabilistic_transitions(
    {},
    excluded_events={"start", "complete", "fail", "retry"},
)
class CloseTaskChart(StateChart):
    pending = State(initial=True)
    in_progress = State()
    failed = State()
    completed = State(final=True)

    start = pending.to(in_progress)
    complete = in_progress.to(completed)
    fail = in_progress.to(failed)
    retry = failed.to(in_progress)


@probabilistic_transitions(
    {},
    excluded_events={"prepare_close", "close", "reopen"},
)
class AccountingPeriodChart(StateChart):
    open = State(initial=True)
    close_ready = State()
    closed = State()
    reopened = State()

    prepare_close = open.to(close_ready) | reopened.to(close_ready)
    close = close_ready.to(closed)
    reopen = closed.to(reopened)

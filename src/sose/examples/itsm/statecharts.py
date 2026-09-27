from statemachine import State, StateChart

from sose.probability import probabilistic_transitions


@probabilistic_transitions(
    {},
    excluded_events={
        "triage",
        "assign",
        "start",
        "resolve",
        "close",
        "escalate",
        "reopen",
    },
)
class IncidentChart(StateChart):
    opened = State(initial=True)
    triaged = State()
    assigned = State()
    in_progress = State()
    escalated = State()
    resolved = State()
    closed = State(final=True)

    triage = opened.to(triaged)
    assign = triaged.to(assigned)
    start = assigned.to(in_progress)
    escalate = (
        triaged.to(escalated)
        | assigned.to(escalated)
        | in_progress.to(escalated)
    )
    resolve = in_progress.to(resolved) | escalated.to(resolved)
    close = resolved.to(closed)
    reopen = resolved.to(in_progress)


@probabilistic_transitions(
    {},
    excluded_events={
        "acknowledge",
        "take_ownership",
        "mitigate",
        "complete",
        "cancel",
    },
)
class EscalationChart(StateChart):
    raised = State(initial=True)
    acknowledged = State()
    owned = State()
    mitigated = State()
    completed = State(final=True)
    cancelled = State(final=True)

    acknowledge = raised.to(acknowledged)
    take_ownership = acknowledged.to(owned)
    mitigate = owned.to(mitigated)
    complete = mitigated.to(completed)
    cancel = (
        raised.to(cancelled)
        | acknowledged.to(cancelled)
        | owned.to(cancelled)
    )

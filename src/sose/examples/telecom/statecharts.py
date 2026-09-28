from statemachine import State, StateChart

from sose.probability import probabilistic_transitions


@probabilistic_transitions(
    {},
    excluded_events={"acknowledge", "start", "complete", "cancel"},
)
class ProductOrderChart(StateChart):
    captured = State(initial=True)
    acknowledged = State()
    in_progress = State()
    completed = State(final=True)
    cancelled = State(final=True)

    acknowledge = captured.to(acknowledged)
    start = acknowledged.to(in_progress)
    complete = in_progress.to(completed)
    cancel = captured.to(cancelled) | acknowledged.to(cancelled) | in_progress.to(cancelled)


@probabilistic_transitions(
    {},
    excluded_events={"accept", "start_provisioning", "complete", "fail"},
)
class ServiceOrderChart(StateChart):
    pending = State(initial=True)
    accepted = State()
    provisioning = State()
    completed = State(final=True)
    failed = State(final=True)

    accept = pending.to(accepted)
    start_provisioning = accepted.to(provisioning)
    complete = provisioning.to(completed)
    fail = accepted.to(failed) | provisioning.to(failed)


@probabilistic_transitions(
    {},
    excluded_events={
        "start_provisioning",
        "make_activation_ready",
        "activate",
        "suspend",
        "restore",
        "terminate",
    },
)
class SubscriptionServiceChart(StateChart):
    designed = State(initial=True)
    provisioning = State()
    activation_ready = State()
    active = State()
    suspended = State()
    terminated = State(final=True)

    start_provisioning = designed.to(provisioning)
    make_activation_ready = provisioning.to(activation_ready)
    activate = activation_ready.to(active)
    suspend = active.to(suspended)
    restore = suspended.to(active)
    terminate = active.to(terminated) | suspended.to(terminated)


@probabilistic_transitions({}, excluded_events={"commit"})
class UsageRecordChart(StateChart):
    captured = State(initial=True)
    committed = State(final=True)

    commit = captured.to(committed)


@probabilistic_transitions({}, excluded_events={"acknowledge", "clear"})
class NetworkAlarmChart(StateChart):
    raised = State(initial=True)
    acknowledged = State()
    cleared = State(final=True)

    acknowledge = raised.to(acknowledged)
    clear = raised.to(cleared) | acknowledged.to(cleared)


@probabilistic_transitions(
    {},
    excluded_events={"acknowledge", "resolve", "close"},
)
class TroubleTicketChart(StateChart):
    open = State(initial=True)
    acknowledged = State()
    resolved = State()
    closed = State(final=True)

    acknowledge = open.to(acknowledged)
    resolve = open.to(resolved) | acknowledged.to(resolved)
    close = resolved.to(closed)

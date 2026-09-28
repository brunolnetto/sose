from statemachine import State, StateChart

from sose.probability import probabilistic_transitions


@probabilistic_transitions(
    {},
    excluded_events={"request_cancel", "withdraw_cancel", "end"},
)
class SubscriptionChart(StateChart):
    active = State(initial=True)
    cancellation_pending = State()
    ended = State(final=True)

    request_cancel = active.to(cancellation_pending)
    withdraw_cancel = cancellation_pending.to(active)
    end = cancellation_pending.to(ended)


@probabilistic_transitions({}, excluded_events={"supersede", "revoke"})
class EntitlementChart(StateChart):
    active = State(initial=True)
    superseded = State(final=True)
    revoked = State(final=True)

    supersede = active.to(superseded)
    revoke = active.to(revoked)


@probabilistic_transitions({}, excluded_events={"apply", "cancel"})
class ChangeRequestChart(StateChart):
    scheduled = State(initial=True)
    applied = State(final=True)
    cancelled = State(final=True)

    apply = scheduled.to(applied)
    cancel = scheduled.to(cancelled)


@probabilistic_transitions({}, excluded_events={"commit"})
class SubscriptionOccurrenceChart(StateChart):
    captured = State(initial=True)
    committed = State(final=True)

    commit = captured.to(committed)

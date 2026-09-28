from statemachine import State, StateChart

from sose.probability import probabilistic_transitions


@probabilistic_transitions(
    {},
    excluded_events={
        "hold",
        "confirm",
        "check_in",
        "check_out",
        "expire_hold",
        "cancel",
        "no_show",
    },
)
class ReservationChart(StateChart):
    requested = State(initial=True)
    held = State()
    confirmed = State()
    checked_in = State()
    checked_out = State(final=True)
    expired = State(final=True)
    cancelled = State(final=True)
    no_show_recorded = State(final=True)

    hold = requested.to(held)
    confirm = held.to(confirmed)
    check_in = confirmed.to(checked_in)
    check_out = checked_in.to(checked_out)
    expire_hold = held.to(expired)
    cancel = held.to(cancelled) | confirmed.to(cancelled)
    no_show = confirmed.to(no_show_recorded)


@probabilistic_transitions(
    {},
    excluded_events={
        "confirm",
        "occupy",
        "complete",
        "expire",
        "release",
        "no_show",
    },
)
class RoomBookingChart(StateChart):
    held = State(initial=True)
    confirmed = State()
    occupied = State()
    completed = State(final=True)
    expired = State(final=True)
    released = State(final=True)
    no_show_recorded = State(final=True)

    confirm = held.to(confirmed)
    occupy = confirmed.to(occupied)
    complete = occupied.to(completed)
    expire = held.to(expired)
    release = held.to(released) | confirmed.to(released)
    no_show = confirmed.to(no_show_recorded)


@probabilistic_transitions({}, excluded_events={"commit"})
class NoShowOccurrenceChart(StateChart):
    captured = State(initial=True)
    committed = State(final=True)

    commit = captured.to(committed)

from statemachine import State, StateChart

from sose.probability import probabilistic_transitions


@probabilistic_transitions({}, excluded_events={"interrupt", "restore", "disconnect"})
class ServicePointChart(StateChart):
    energized = State(initial=True)
    interrupted = State()
    disconnected = State(final=True)

    interrupt = energized.to(interrupted)
    restore = interrupted.to(energized)
    disconnect = energized.to(disconnected) | interrupted.to(disconnected)


@probabilistic_transitions({}, excluded_events={"retire"})
class MeterChart(StateChart):
    active = State(initial=True)
    retired = State(final=True)

    retire = active.to(retired)


@probabilistic_transitions({}, excluded_events={"commit"})
class MeterReadingChart(StateChart):
    captured = State(initial=True)
    committed = State(final=True)

    commit = captured.to(committed)


@probabilistic_transitions(
    {},
    excluded_events={"confirm", "begin_restoration", "restore"},
)
class OutageChart(StateChart):
    reported = State(initial=True)
    confirmed = State()
    restoring = State()
    restored = State(final=True)

    confirm = reported.to(confirmed)
    begin_restoration = confirmed.to(restoring)
    restore = restoring.to(restored)


@probabilistic_transitions({}, excluded_events={"start", "finish", "cancel"})
class DemandResponseEventChart(StateChart):
    scheduled = State(initial=True)
    active = State()
    completed = State(final=True)
    cancelled = State(final=True)

    start = scheduled.to(active)
    finish = active.to(completed)
    cancel = scheduled.to(cancelled) | active.to(cancelled)


@probabilistic_transitions(
    {},
    excluded_events={"begin", "complete", "miss", "opt_out"},
)
class DemandResponseParticipationChart(StateChart):
    eligible = State(initial=True)
    active = State()
    completed = State(final=True)
    missed = State(final=True)
    opted_out = State(final=True)

    begin = eligible.to(active)
    complete = active.to(completed)
    miss = eligible.to(missed)
    opt_out = eligible.to(opted_out) | active.to(opted_out)

from statemachine import State, StateChart

from sose.probability import probabilistic_transitions


@probabilistic_transitions({}, excluded_events={"assign", "release", "withdraw", "restore"})
class VehicleChart(StateChart):
    idle = State(initial=True)
    in_service = State()
    out_of_service = State()

    assign = idle.to(in_service)
    release = in_service.to(idle)
    withdraw = idle.to(out_of_service) | in_service.to(out_of_service)
    restore = out_of_service.to(idle)


@probabilistic_transitions({}, excluded_events={"start", "complete", "cancel"})
class VehicleBlockChart(StateChart):
    planned = State(initial=True)
    active = State()
    completed = State(final=True)
    cancelled = State(final=True)

    start = planned.to(active)
    complete = active.to(completed)
    cancel = planned.to(cancelled) | active.to(cancelled)


@probabilistic_transitions({}, excluded_events={"start", "complete", "cancel"})
class ScheduledTripChart(StateChart):
    planned = State(initial=True)
    in_progress = State()
    completed = State(final=True)
    cancelled = State(final=True)

    start = planned.to(in_progress)
    complete = in_progress.to(completed)
    cancel = planned.to(cancelled) | in_progress.to(cancelled)


@probabilistic_transitions({}, excluded_events={"arrive", "depart", "skip"})
class StopCallChart(StateChart):
    pending = State(initial=True)
    arrived = State()
    departed = State(final=True)
    skipped = State(final=True)

    arrive = pending.to(arrived)
    depart = arrived.to(departed)
    skip = pending.to(skipped)


@probabilistic_transitions({}, excluded_events={"commit"})
class TripUpdateChart(StateChart):
    captured = State(initial=True)
    committed = State(final=True)

    commit = captured.to(committed)


@probabilistic_transitions({}, excluded_events={"commit"})
class VehiclePositionChart(StateChart):
    captured = State(initial=True)
    committed = State(final=True)

    commit = captured.to(committed)


@probabilistic_transitions({}, excluded_events={"activate", "resolve", "cancel"})
class ServiceAlertChart(StateChart):
    scheduled = State(initial=True)
    active = State()
    resolved = State(final=True)
    cancelled = State(final=True)

    activate = scheduled.to(active)
    resolve = active.to(resolved)
    cancel = scheduled.to(cancelled) | active.to(cancelled)

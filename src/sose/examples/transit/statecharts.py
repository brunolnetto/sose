from statemachine import State, StateChart

from sose.probability import probabilistic_transitions


@probabilistic_transitions({}, excluded_events={"assign", "release", "retire"})
class VehicleChart(StateChart):
    available = State(initial=True)
    in_service = State()
    retired = State(final=True)

    assign = available.to(in_service)
    release = in_service.to(available)
    retire = available.to(retired)


@probabilistic_transitions({}, excluded_events={"start", "complete", "cancel"})
class ScheduledTripChart(StateChart):
    planned = State(initial=True)
    running = State()
    completed = State(final=True)
    cancelled = State(final=True)

    start = planned.to(running)
    complete = running.to(completed)
    cancel = planned.to(cancelled) | running.to(cancelled)


@probabilistic_transitions({}, excluded_events={"commit"})
class TripUpdateOccurrenceChart(StateChart):
    captured = State(initial=True)
    committed = State(final=True)

    commit = captured.to(committed)


@probabilistic_transitions({}, excluded_events={"commit"})
class VehiclePositionOccurrenceChart(StateChart):
    captured = State(initial=True)
    committed = State(final=True)

    commit = captured.to(committed)


@probabilistic_transitions({}, excluded_events={"activate", "clear", "cancel"})
class ServiceAlertChart(StateChart):
    scheduled = State(initial=True)
    active = State()
    cleared = State(final=True)
    cancelled = State(final=True)

    activate = scheduled.to(active)
    clear = active.to(cleared)
    cancel = scheduled.to(cancelled) | active.to(cancelled)

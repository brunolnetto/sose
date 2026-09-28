from statemachine import State, StateChart

from sose.probability import probabilistic_transitions


@probabilistic_transitions(
    {},
    excluded_events={"schedule", "start", "require_reschedule", "reschedule", "complete", "cancel"},
)
class WorkOrderChart(StateChart):
    ready = State(initial=True)
    scheduled = State()
    in_progress = State()
    reschedule_required = State()
    completed = State(final=True)
    cancelled = State(final=True)

    schedule = ready.to(scheduled)
    start = scheduled.to(in_progress)
    require_reschedule = scheduled.to(reschedule_required) | in_progress.to(reschedule_required)
    reschedule = reschedule_required.to(scheduled)
    complete = in_progress.to(completed)
    cancel = ready.to(cancelled) | scheduled.to(cancelled) | reschedule_required.to(cancelled)


@probabilistic_transitions(
    {},
    excluded_events={"confirm", "start", "complete", "no_access", "miss", "cancel"},
)
class AppointmentChart(StateChart):
    proposed = State(initial=True)
    confirmed = State()
    in_progress = State()
    completed = State(final=True)
    no_access_recorded = State(final=True)
    missed = State(final=True)
    cancelled = State(final=True)

    confirm = proposed.to(confirmed)
    start = confirmed.to(in_progress)
    complete = in_progress.to(completed)
    no_access = in_progress.to(no_access_recorded)
    miss = confirmed.to(missed) | in_progress.to(no_access_recorded)
    cancel = proposed.to(cancelled) | confirmed.to(cancelled)


@probabilistic_transitions({}, excluded_events={"assign", "release"})
class TechnicianChart(StateChart):
    available = State(initial=True)
    assigned = State()

    assign = available.to(assigned)
    release = assigned.to(available)


@probabilistic_transitions({}, excluded_events={"commit"})
class VisitOccurrenceChart(StateChart):
    captured = State(initial=True)
    committed = State(final=True)

    commit = captured.to(committed)

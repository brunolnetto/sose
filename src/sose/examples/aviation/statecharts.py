from statemachine import State, StateChart

from sose.probability import probabilistic_transitions


@probabilistic_transitions(
    {},
    excluded_events={
        "assign",
        "dispatch",
        "land",
        "release",
        "mark_aog",
        "start_maintenance",
        "finish_maintenance",
    },
)
class AircraftChart(StateChart):
    available = State(initial=True)
    assigned = State()
    airborne = State()
    inspection = State()
    aog = State()
    maintenance = State()
    released = State()

    assign = available.to(assigned) | released.to(assigned)
    dispatch = assigned.to(airborne)
    land = airborne.to(inspection)
    release = inspection.to(released)
    mark_aog = inspection.to(aog)
    start_maintenance = aog.to(maintenance)
    finish_maintenance = maintenance.to(released)


@probabilistic_transitions(
    {},
    excluded_events={
        "make_due",
        "delay",
        "resume",
        "mark_ready",
        "depart",
        "land",
        "inspect",
        "release",
        "divert",
        "cancel",
    },
)
class FlightChart(StateChart):
    scheduled = State(initial=True)
    due = State()
    delayed = State()
    ready = State()
    airborne = State()
    landed = State()
    inspection = State()
    released = State(final=True)
    diverted = State(final=True)
    cancelled = State(final=True)

    make_due = scheduled.to(due)
    delay = due.to(delayed) | ready.to(delayed)
    resume = delayed.to(due)
    mark_ready = due.to(ready)
    depart = ready.to(airborne)
    land = airborne.to(landed)
    inspect = landed.to(inspection)
    release = inspection.to(released)
    divert = airborne.to(diverted)
    cancel = scheduled.to(cancelled) | due.to(cancelled) | delayed.to(cancelled)


@probabilistic_transitions(
    {},
    excluded_events={"reserve", "activate", "release", "cancel"},
)
class CrewAssignmentChart(StateChart):
    planned = State(initial=True)
    reserved = State()
    active = State()
    released = State(final=True)
    cancelled = State(final=True)

    reserve = planned.to(reserved)
    activate = reserved.to(active)
    release = active.to(released)
    cancel = planned.to(cancelled) | reserved.to(cancelled)


@probabilistic_transitions(
    {},
    excluded_events={"begin", "pass_inspection", "fail_inspection"},
)
class InspectionChart(StateChart):
    pending = State(initial=True)
    inspecting = State()
    passed = State(final=True)
    failed = State(final=True)

    begin = pending.to(inspecting)
    pass_inspection = inspecting.to(passed)
    fail_inspection = inspecting.to(failed)


@probabilistic_transitions(
    {},
    excluded_events={
        "release",
        "wait_part",
        "part_ready",
        "wait_bay",
        "start",
        "interrupt",
        "resume",
        "complete",
    },
)
class MaintenanceWorkOrderChart(StateChart):
    planned = State(initial=True)
    released = State()
    waiting_part = State()
    waiting_bay = State()
    in_progress = State()
    interrupted = State()
    completed = State(final=True)

    release = planned.to(released)
    wait_part = released.to(waiting_part)
    part_ready = waiting_part.to(released)
    wait_bay = released.to(waiting_bay)
    start = waiting_bay.to(in_progress)
    interrupt = in_progress.to(interrupted)
    resume = interrupted.to(in_progress)
    complete = in_progress.to(completed)


@probabilistic_transitions(
    {},
    excluded_events={"allocate", "issue"},
)
class PartDemandChart(StateChart):
    open = State(initial=True)
    allocated = State()
    issued = State(final=True)

    allocate = open.to(allocated)
    issue = allocated.to(issued)

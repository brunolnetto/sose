from statemachine import State, StateChart

from sose.probability import probabilistic_transitions


@probabilistic_transitions(
    {},
    excluded_events={
        "arrive",
        "assign_gate",
        "hold_gate",
        "reallocate_gate",
        "start_deboarding",
        "start_servicing",
        "service_ready",
        "start_boarding",
        "baggage_delayed",
        "baggage_ready",
        "slot_ready",
        "depart",
        "cancel",
    },
)
class FlightTurnaroundChart(StateChart):
    scheduled = State(initial=True)
    arrived = State()
    gate_hold = State()
    gate_assigned = State()
    deboarding = State()
    servicing = State()
    boarding = State()
    waiting_baggage = State()
    waiting_slot = State()
    pushback = State()
    departed = State(final=True)
    cancelled = State(final=True)

    arrive = scheduled.to(arrived)
    assign_gate = arrived.to(gate_assigned) | gate_hold.to(gate_assigned)
    hold_gate = arrived.to(gate_hold)
    reallocate_gate = gate_assigned.to(gate_hold)
    start_deboarding = gate_assigned.to(deboarding)
    start_servicing = deboarding.to(servicing)
    service_ready = servicing.to(boarding)
    baggage_delayed = boarding.to(waiting_baggage)
    baggage_ready = waiting_baggage.to(boarding)
    start_boarding = boarding.to(waiting_slot)
    slot_ready = waiting_slot.to(pushback)
    depart = pushback.to(departed)
    cancel = (
        scheduled.to(cancelled)
        | arrived.to(cancelled)
        | gate_hold.to(cancelled)
        | gate_assigned.to(cancelled)
        | deboarding.to(cancelled)
        | servicing.to(cancelled)
        | boarding.to(cancelled)
        | waiting_baggage.to(cancelled)
        | waiting_slot.to(cancelled)
    )


@probabilistic_transitions(
    {},
    excluded_events={"reserve", "occupy", "release", "reallocate"},
)
class GateAssignmentChart(StateChart):
    planned = State(initial=True)
    reserved = State()
    occupied = State()
    released = State(final=True)
    reallocated = State()

    reserve = planned.to(reserved) | reallocated.to(reserved)
    occupy = reserved.to(occupied)
    release = occupied.to(released)
    reallocate = reserved.to(reallocated) | occupied.to(reallocated)


@probabilistic_transitions(
    {},
    excluded_events={"start", "complete", "fail", "retry"},
)
class GroundServiceTaskChart(StateChart):
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
    excluded_events={"start", "mark_ready", "delay"},
)
class BaggageFlowChart(StateChart):
    pending = State(initial=True)
    transferring = State()
    delayed = State()
    ready = State(final=True)

    start = pending.to(transferring)
    mark_ready = transferring.to(ready) | delayed.to(ready)
    delay = transferring.to(delayed)


@probabilistic_transitions(
    {},
    excluded_events={"schedule", "make_due", "delay", "consume"},
)
class DepartureSlotChart(StateChart):
    planned = State(initial=True)
    scheduled = State()
    due = State()
    delayed = State()
    consumed = State(final=True)

    schedule = planned.to(scheduled)
    make_due = scheduled.to(due)
    delay = due.to(delayed)
    consume = due.to(consumed) | delayed.to(consumed)

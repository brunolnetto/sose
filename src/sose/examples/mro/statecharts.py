from statemachine import State, StateChart

from sose.probability import probabilistic_transitions


@probabilistic_transitions(
    {
        # From PLANNED: release vs cancellation.
        "release": 0.98,
        # From RELEASED: start / wait / cancel normalize to 0.78 / 0.20 / 0.02.
        "start": 0.78,
        "wait_for_material": 0.20,
        "cancel": 0.02,
        # Single-path lifecycle steps remain deterministic after guard filtering.
        "complete": 1.0,
        "close": 1.0,
    },
    strict=False,
)
class WorkOrderChart(StateChart):
    planned = State(initial=True)
    released = State()
    waiting_material = State()
    in_progress = State()
    completed = State()
    closed = State(final=True)
    cancelled = State(final=True)

    release = planned.to(released)
    wait_for_material = released.to(waiting_material)
    start = released.to(in_progress) | waiting_material.to(in_progress)
    complete = in_progress.to(completed)
    close = completed.to(closed)
    cancel = (
        planned.to(cancelled)
        | released.to(cancelled)
        | waiting_material.to(cancelled)
    )

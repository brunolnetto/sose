from statemachine import State, StateChart

from sose.probability import probabilistic_transitions


@probabilistic_transitions(
    {
        "release": 0.98,
        "start": 0.78,
        "wait_for_material": 0.20,
        "wait_for_resource": 0.20,
        "cancel": 0.02,
        "complete": 1.0,
        "close": 1.0,
    },
    strict=False,
)
class WorkOrderChart(StateChart):
    planned = State(initial=True)
    released = State()
    waiting_material = State()
    waiting_resource = State()
    in_progress = State()
    interrupted = State()
    completed = State()
    closed = State(final=True)
    cancelled = State(final=True)

    release = planned.to(released)
    wait_for_material = released.to(waiting_material) | waiting_resource.to(
        waiting_material
    )
    wait_for_resource = released.to(waiting_resource)
    material_ready = waiting_material.to(released)
    resource_ready = waiting_resource.to(released)
    start = released.to(in_progress)
    interrupt = in_progress.to(interrupted)
    resume = interrupted.to(in_progress)
    complete = in_progress.to(completed)
    close = completed.to(closed)
    cancel = (
        planned.to(cancelled)
        | released.to(cancelled)
        | waiting_material.to(cancelled)
        | waiting_resource.to(cancelled)
    )


class PartDemandChart(StateChart):
    open = State(initial=True)
    waiting_inventory = State()
    allocated = State()
    consumed = State(final=True)
    cancelled = State(final=True)

    wait = open.to(waiting_inventory)
    allocate = open.to(allocated) | waiting_inventory.to(allocated)
    consume = allocated.to(consumed)
    cancel = open.to(cancelled) | waiting_inventory.to(cancelled)

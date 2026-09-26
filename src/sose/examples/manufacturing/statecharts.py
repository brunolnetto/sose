from statemachine import State, StateChart


class ProductionOrderChart(StateChart):
    planned = State(initial=True)
    released = State()
    waiting_material = State()
    setup = State()
    producing = State()
    machine_down = State()
    inspection = State()
    quality_hold = State()
    rework = State()
    completed = State(final=True)
    cancelled = State(final=True)

    release = planned.to(released)
    wait_for_material = released.to(waiting_material)
    material_ready = waiting_material.to(released)
    begin_setup = released.to(setup)
    start_production = setup.to(producing)
    breakdown = setup.to(machine_down) | producing.to(machine_down)
    repair = machine_down.to(setup)
    begin_inspection = producing.to(inspection)
    hold_quality = inspection.to(quality_hold)
    rework_order = quality_hold.to(rework)
    resume_rework = rework.to(producing)
    complete = inspection.to(completed)
    cancel = planned.to(cancelled) | released.to(cancelled) | waiting_material.to(cancelled)


class OperationChart(StateChart):
    pending = State(initial=True)
    ready_state = State()
    running = State()
    blocked = State()
    done = State(final=True)

    ready = pending.to(ready_state)
    start = ready_state.to(running)
    block = ready_state.to(blocked) | running.to(blocked)
    unblock = blocked.to(ready_state)
    finish = running.to(done)

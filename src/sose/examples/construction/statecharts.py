from statemachine import State, StateChart

from sose.probability import probabilistic_transitions


@probabilistic_transitions(
    {},
    excluded_events={
        "block_dependency",
        "release",
        "dependency_ready",
        "wait_material",
        "material_ready",
        "request_resources",
        "start",
        "finish_work",
        "accept",
        "reject",
        "schedule_rework",
        "measure",
        "complete",
        "cancel",
    },
)
class ActivityChart(StateChart):
    planned = State(initial=True)
    blocked_dependency = State()
    ready = State()
    waiting_material = State()
    waiting_resource = State()
    executing = State()
    inspection = State()
    rework = State()
    measured = State()
    completed = State(final=True)
    cancelled = State(final=True)

    block_dependency = planned.to(blocked_dependency)
    release = planned.to(ready)
    dependency_ready = blocked_dependency.to(ready)
    wait_material = ready.to(waiting_material)
    material_ready = waiting_material.to(ready)
    request_resources = ready.to(waiting_resource)
    start = waiting_resource.to(executing)
    finish_work = executing.to(inspection)
    accept = inspection.to(measured)
    reject = inspection.to(rework)
    schedule_rework = rework.to(waiting_resource)
    measure = measured.to(measured, internal=True)
    complete = measured.to(completed)
    cancel = (
        planned.to(cancelled)
        | blocked_dependency.to(cancelled)
        | ready.to(cancelled)
        | waiting_material.to(cancelled)
        | waiting_resource.to(cancelled)
    )


@probabilistic_transitions(
    {},
    excluded_events={"begin", "pass_inspection", "fail_inspection", "void"},
)
class InspectionChart(StateChart):
    pending = State(initial=True)
    inspecting = State()
    passed = State(final=True)
    failed = State(final=True)
    voided = State(final=True)

    begin = pending.to(inspecting)
    pass_inspection = inspecting.to(passed)
    fail_inspection = inspecting.to(failed)
    void = pending.to(voided)

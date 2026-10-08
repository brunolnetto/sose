from statemachine import State, StateChart

from sose.probability import probabilistic_transitions


@probabilistic_transitions(
    {},
    excluded_events={"allocate", "start_pick", "pack", "ship", "cancel"},
)
class FulfillmentOrderChart(StateChart):
    requested = State(initial=True)
    allocated = State()
    picking = State()
    packed = State()
    shipped = State(final=True)
    cancelled = State(final=True)

    allocate = requested.to(allocated)
    start_pick = allocated.to(picking)
    pack = picking.to(packed)
    ship = packed.to(shipped)
    cancel = requested.to(cancelled) | allocated.to(cancelled)


@probabilistic_transitions({}, excluded_events={"pick", "ship", "release"})
class AllocationChart(StateChart):
    committed = State(initial=True)
    picked = State()
    shipped = State(final=True)
    released = State(final=True)

    pick = committed.to(picked)
    ship = picked.to(shipped)
    release = committed.to(released)


@probabilistic_transitions({}, excluded_events={"commit"})
class InventoryOccurrenceChart(StateChart):
    captured = State(initial=True)
    committed = State(final=True)

    commit = captured.to(committed)


@probabilistic_transitions({}, excluded_events={"start", "complete"})
class FulfillmentServiceTaskChart(StateChart):
    queued = State(initial=True)
    in_progress = State()
    completed = State(final=True)

    start = queued.to(in_progress)
    complete = in_progress.to(completed)

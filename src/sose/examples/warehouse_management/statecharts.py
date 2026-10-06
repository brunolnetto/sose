from statemachine import State, StateChart

from sose.probability import probabilistic_transitions


@probabilistic_transitions({}, excluded_events={"disrupt", "restore"})
class WarehouseSiteChart(StateChart):
    operational = State(initial=True)
    disrupted = State()

    disrupt = operational.to(disrupted)
    restore = disrupted.to(operational)


@probabilistic_transitions({}, excluded_events={"reserve", "occupy", "release"})
class DockChart(StateChart):
    available = State(initial=True)
    reserved = State()
    occupied = State()

    reserve = available.to(reserved)
    occupy = reserved.to(occupied)
    release = occupied.to(available) | reserved.to(available)


@probabilistic_transitions({}, excluded_events={"depart_origin", "wait_for_dock", "dock", "release"})
class TruckChart(StateChart):
    scheduled = State(initial=True)
    in_transit = State()
    waiting_dock = State()
    docked = State()
    released = State(final=True)

    depart_origin = scheduled.to(in_transit)
    wait_for_dock = in_transit.to(waiting_dock)
    dock = in_transit.to(docked) | waiting_dock.to(docked)
    release = docked.to(released)


@probabilistic_transitions({}, excluded_events={"assign", "release"})
class ForkliftChart(StateChart):
    available = State(initial=True)
    assigned = State()

    assign = available.to(assigned)
    release = assigned.to(available)


@probabilistic_transitions(
    {},
    excluded_events={"start_transit", "arrive", "delay", "resume", "dock", "start_handling", "complete"},
)
class ShipmentChart(StateChart):
    planned = State(initial=True)
    in_transit = State()
    arrived = State()
    delayed = State()
    docked = State()
    handling = State()
    completed = State(final=True)

    start_transit = planned.to(in_transit)
    arrive = in_transit.to(arrived)
    delay = arrived.to(delayed)
    resume = delayed.to(arrived)
    dock = arrived.to(docked)
    start_handling = docked.to(handling)
    complete = handling.to(completed)

from sose.examples.airports.entities import (
    BaggageFlow,
    DepartureSlot,
    FlightTurnaround,
    GateAssignment,
    GroundServiceTask,
)
from sose.examples.airports.statecharts import (
    BaggageFlowChart,
    DepartureSlotChart,
    FlightTurnaroundChart,
    GateAssignmentChart,
    GroundServiceTaskChart,
)
from sose.statecharts.topology import graph_from_statechart, policy_from_statechart


def test_airport_entities_have_stable_types():
    assert FlightTurnaround(id="t").entity_type == "airport_flight_turnaround"
    assert GateAssignment(id="g").entity_type == "airport_gate_assignment"
    assert GroundServiceTask(id="s").entity_type == "airport_ground_service_task"
    assert BaggageFlow(id="b").entity_type == "airport_baggage_flow"
    assert DepartureSlot(id="d").entity_type == "airport_departure_slot"


def test_turnaround_topology_separates_gate_baggage_and_slot_waiting():
    graph = graph_from_statechart(FlightTurnaroundChart())
    edges = {(e.source, e.event, e.targets) for e in graph.edges}
    assert ("scheduled", "arrive", ("arrived",)) in edges
    assert ("arrived", "hold_gate", ("gate_hold",)) in edges
    assert ("gate_hold", "assign_gate", ("gate_assigned",)) in edges
    assert ("gate_assigned", "reallocate_gate", ("gate_hold",)) in edges
    assert ("servicing", "service_ready", ("boarding",)) in edges
    assert ("boarding", "baggage_delayed", ("waiting_baggage",)) in edges
    assert ("waiting_baggage", "baggage_ready", ("boarding",)) in edges
    assert ("boarding", "start_boarding", ("waiting_slot",)) in edges
    assert ("waiting_slot", "slot_ready", ("pushback",)) in edges
    assert ("pushback", "depart", ("departed",)) in edges


def test_slot_due_is_distinct_from_slot_consumption():
    graph = graph_from_statechart(DepartureSlotChart())
    edges = {(e.source, e.event, e.targets) for e in graph.edges}
    assert ("planned", "schedule", ("scheduled",)) in edges
    assert ("scheduled", "make_due", ("due",)) in edges
    assert ("due", "delay", ("delayed",)) in edges
    assert ("due", "consume", ("consumed",)) in edges
    assert ("delayed", "consume", ("consumed",)) in edges


def test_airport_statecharts_are_orchestration_gated():
    for chart in (
        FlightTurnaroundChart(),
        GateAssignmentChart(),
        GroundServiceTaskChart(),
        BaggageFlowChart(),
        DepartureSlotChart(),
    ):
        policy = policy_from_statechart(chart)
        assert policy.events == ()
        for edge in graph_from_statechart(chart).edges:
            if edge.event is not None:
                assert policy.is_probabilistically_eligible(edge.event) is False

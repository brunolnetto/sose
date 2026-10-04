from sose.examples.aviation.entities import (
    Aircraft,
    CrewAssignment,
    Flight,
    Inspection,
    MaintenanceWorkOrder,
    PartDemand,
)
from sose.examples.aviation.statecharts import (
    AircraftChart,
    CrewAssignmentChart,
    FlightChart,
    InspectionChart,
    MaintenanceWorkOrderChart,
    PartDemandChart,
)
from sose.statecharts.topology import graph_from_statechart, policy_from_statechart


def test_aviation_entities_have_stable_types():
    assert Aircraft(id="a").entity_type == "aviation_aircraft"
    assert Flight(id="f").entity_type == "aviation_flight"
    assert CrewAssignment(id="c").entity_type == "aviation_crew_assignment"
    assert Inspection(id="i").entity_type == "aviation_inspection"
    assert MaintenanceWorkOrder(id="m").entity_type == "aviation_maintenance_work_order"
    assert PartDemand(id="p").entity_type == "aviation_part_demand"


def test_flight_topology_separates_due_delay_and_airworthiness_release():
    graph = graph_from_statechart(FlightChart())
    edges = {(e.source, e.event, e.targets) for e in graph.edges}
    assert ("scheduled", "make_due", ("due",)) in edges
    assert ("due", "delay", ("delayed",)) in edges
    assert ("delayed", "resume", ("due",)) in edges
    assert ("due", "mark_ready", ("ready",)) in edges
    assert ("ready", "depart", ("airborne",)) in edges
    assert ("airborne", "land", ("landed",)) in edges
    assert ("landed", "inspect", ("inspection",)) in edges
    assert ("inspection", "release", ("released",)) in edges
    assert ("airborne", "divert", ("diverted",)) in edges


def test_maintenance_topology_contains_part_and_preemption_states():
    graph = graph_from_statechart(MaintenanceWorkOrderChart())
    edges = {(e.source, e.event, e.targets) for e in graph.edges}
    assert ("released", "wait_part", ("waiting_part",)) in edges
    assert ("waiting_part", "part_ready", ("released",)) in edges
    assert ("released", "wait_bay", ("waiting_bay",)) in edges
    assert ("waiting_bay", "start", ("in_progress",)) in edges
    assert ("in_progress", "interrupt", ("interrupted",)) in edges
    assert ("interrupted", "resume", ("in_progress",)) in edges


def test_aviation_statecharts_are_orchestration_gated():
    for chart in (
        AircraftChart(),
        FlightChart(),
        CrewAssignmentChart(),
        InspectionChart(),
        MaintenanceWorkOrderChart(),
        PartDemandChart(),
    ):
        policy = policy_from_statechart(chart)
        assert policy.events == ()
        for edge in graph_from_statechart(chart).edges:
            if edge.event is not None:
                assert policy.is_probabilistically_eligible(edge.event) is False

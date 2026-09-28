from sose.examples.public_transit.entities import (
    ScheduledTrip,
    ServiceAlert,
    StopCall,
    TripUpdate,
    Vehicle,
    VehicleBlock,
    VehiclePositionOccurrence,
)
from sose.examples.public_transit.statecharts import (
    ScheduledTripChart,
    ServiceAlertChart,
    StopCallChart,
    TripUpdateChart,
    VehicleBlockChart,
    VehicleChart,
    VehiclePositionChart,
)
from sose.statecharts.topology import graph_from_statechart, policy_from_statechart


def _edges(chart):
    graph = graph_from_statechart(chart)
    return {(edge.source, edge.event, edge.targets) for edge in graph.edges}


def test_public_transit_entities_have_stable_types():
    assert Vehicle(id="v").entity_type == "transit_vehicle"
    assert VehicleBlock(id="b").entity_type == "transit_vehicle_block"
    assert ScheduledTrip(id="t").entity_type == "transit_scheduled_trip"
    assert StopCall(id="s").entity_type == "transit_stop_call"
    assert TripUpdate(id="u").entity_type == "transit_trip_update"
    assert VehiclePositionOccurrence(id="p").entity_type == "transit_vehicle_position"
    assert ServiceAlert(id="a").entity_type == "transit_service_alert"


def test_schedule_projection_and_occurrence_lifecycles_are_separate():
    assert ("planned", "start", ("in_progress",)) in _edges(ScheduledTripChart())
    assert ("pending", "arrive", ("arrived",)) in _edges(StopCallChart())
    assert ("captured", "commit", ("committed",)) in _edges(TripUpdateChart())
    assert ("captured", "commit", ("committed",)) in _edges(VehiclePositionChart())
    assert ("scheduled", "activate", ("active",)) in _edges(ServiceAlertChart())
    assert ("active", "resolve", ("resolved",)) in _edges(ServiceAlertChart())


def test_block_and_vehicle_ownership_are_explicit():
    assert ("planned", "start", ("active",)) in _edges(VehicleBlockChart())
    assert ("active", "complete", ("completed",)) in _edges(VehicleBlockChart())
    assert ("idle", "assign", ("in_service",)) in _edges(VehicleChart())
    assert ("in_service", "release", ("idle",)) in _edges(VehicleChart())


def test_public_transit_reference_transitions_are_orchestration_gated():
    for chart in (
        VehicleChart(),
        VehicleBlockChart(),
        ScheduledTripChart(),
        StopCallChart(),
        TripUpdateChart(),
        VehiclePositionChart(),
        ServiceAlertChart(),
    ):
        assert policy_from_statechart(chart).events == ()

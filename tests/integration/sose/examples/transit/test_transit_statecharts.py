from sose.examples.transit.entities import (
    ScheduledTrip,
    ServiceAlert,
    TripUpdateOccurrence,
    Vehicle,
    VehiclePositionOccurrence,
)
from sose.examples.transit.statecharts import (
    ScheduledTripChart,
    ServiceAlertChart,
    TripUpdateOccurrenceChart,
    VehicleChart,
    VehiclePositionOccurrenceChart,
)
from sose.statecharts.topology import graph_from_statechart, policy_from_statechart


def _edges(chart):
    graph = graph_from_statechart(chart)
    return {(edge.source, edge.event, edge.targets) for edge in graph.edges}


def test_transit_entities_have_stable_types():
    assert Vehicle(id="v").entity_type == "transit_vehicle"
    assert ScheduledTrip(id="t").entity_type == "transit_scheduled_trip"
    assert TripUpdateOccurrence(id="u").entity_type == "transit_trip_update"
    assert VehiclePositionOccurrence(id="p").entity_type == "transit_vehicle_position"
    assert ServiceAlert(id="a").entity_type == "transit_service_alert"


def test_schedule_realtime_and_alert_lifecycles_are_separate():
    assert ("planned", "start", ("running",)) in _edges(ScheduledTripChart())
    assert ("running", "complete", ("completed",)) in _edges(ScheduledTripChart())
    assert ("captured", "commit", ("committed",)) in _edges(
        TripUpdateOccurrenceChart()
    )
    assert ("captured", "commit", ("committed",)) in _edges(
        VehiclePositionOccurrenceChart()
    )
    assert ("scheduled", "activate", ("active",)) in _edges(ServiceAlertChart())
    assert ("active", "clear", ("cleared",)) in _edges(ServiceAlertChart())


def test_transit_reference_transitions_are_orchestration_gated():
    for chart in (
        VehicleChart(),
        ScheduledTripChart(),
        TripUpdateOccurrenceChart(),
        VehiclePositionOccurrenceChart(),
        ServiceAlertChart(),
    ):
        assert policy_from_statechart(chart).events == ()

from sose.examples.hospitality.entities import (
    Hotel,
    NoShowOccurrence,
    Reservation,
    Room,
    RoomBooking,
)
from sose.examples.hospitality.statecharts import (
    NoShowOccurrenceChart,
    ReservationChart,
    RoomBookingChart,
)
from sose.statecharts.topology import graph_from_statechart, policy_from_statechart


def _edges(chart):
    graph = graph_from_statechart(chart)
    return {(edge.source, edge.event, edge.targets) for edge in graph.edges}


def test_hospitality_entities_have_stable_types():
    assert Hotel(id="h").entity_type == "hospitality_hotel"
    assert Room(id="r").entity_type == "hospitality_room"
    assert Reservation(id="x").entity_type == "hospitality_reservation"
    assert RoomBooking(id="b").entity_type == "hospitality_room_booking"
    assert NoShowOccurrence(id="n").entity_type == "hospitality_no_show_occurrence"


def test_reservation_and_booking_keep_commercial_and_inventory_truth_separate():
    assert ("held", "confirm", ("confirmed",)) in _edges(ReservationChart())
    assert ("held", "expire_hold", ("expired",)) in _edges(ReservationChart())
    assert ("confirmed", "no_show", ("no_show_recorded",)) in _edges(ReservationChart())
    assert ("held", "expire", ("expired",)) in _edges(RoomBookingChart())
    assert ("confirmed", "no_show", ("no_show_recorded",)) in _edges(RoomBookingChart())
    assert ("captured", "commit", ("committed",)) in _edges(NoShowOccurrenceChart())


def test_hospitality_transitions_are_orchestration_gated():
    for chart in (
        ReservationChart(),
        RoomBookingChart(),
        NoShowOccurrenceChart(),
    ):
        assert policy_from_statechart(chart).events == ()

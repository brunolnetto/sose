from sose.examples.energy_utilities.entities import (
    DemandResponseEvent,
    DemandResponseParticipation,
    Meter,
    MeterReading,
    Outage,
    ServicePoint,
)
from sose.examples.energy_utilities.statecharts import (
    DemandResponseEventChart,
    DemandResponseParticipationChart,
    MeterChart,
    MeterReadingChart,
    OutageChart,
    ServicePointChart,
)
from sose.statecharts.topology import graph_from_statechart, policy_from_statechart


def _edges(chart):
    graph = graph_from_statechart(chart)
    return {(edge.source, edge.event, edge.targets) for edge in graph.edges}


def test_energy_entities_have_stable_types():
    assert ServicePoint(id="sp").entity_type == "utility_service_point"
    assert Meter(id="m").entity_type == "utility_meter"
    assert MeterReading(id="r").entity_type == "utility_meter_reading"
    assert Outage(id="o").entity_type == "utility_outage"
    assert DemandResponseEvent(id="e").entity_type == "utility_dr_event"
    assert DemandResponseParticipation(id="p").entity_type == "utility_dr_participation"


def test_energy_statecharts_separate_measurement_outage_and_dr_lifecycles():
    assert ("energized", "interrupt", ("interrupted",)) in _edges(ServicePointChart())
    assert ("interrupted", "restore", ("energized",)) in _edges(ServicePointChart())
    assert ("captured", "commit", ("committed",)) in _edges(MeterReadingChart())
    assert ("reported", "confirm", ("confirmed",)) in _edges(OutageChart())
    assert ("confirmed", "begin_restoration", ("restoring",)) in _edges(OutageChart())
    assert ("restoring", "restore", ("restored",)) in _edges(OutageChart())
    assert ("scheduled", "start", ("active",)) in _edges(DemandResponseEventChart())
    assert ("active", "finish", ("completed",)) in _edges(DemandResponseEventChart())
    assert ("eligible", "begin", ("active",)) in _edges(DemandResponseParticipationChart())
    assert ("active", "complete", ("completed",)) in _edges(DemandResponseParticipationChart())


def test_energy_reference_transitions_are_orchestration_gated():
    for chart in (
        ServicePointChart(),
        MeterChart(),
        MeterReadingChart(),
        OutageChart(),
        DemandResponseEventChart(),
        DemandResponseParticipationChart(),
    ):
        assert policy_from_statechart(chart).events == ()

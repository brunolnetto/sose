from sose.examples.field_service.entities import (
    Appointment,
    Technician,
    VisitOccurrence,
    WorkOrder,
)
from sose.examples.field_service.statecharts import (
    AppointmentChart,
    TechnicianChart,
    VisitOccurrenceChart,
    WorkOrderChart,
)
from sose.statecharts.topology import graph_from_statechart, policy_from_statechart


def _edges(chart):
    graph = graph_from_statechart(chart)
    return {(edge.source, edge.event, edge.targets) for edge in graph.edges}


def test_field_service_entities_have_stable_types():
    assert WorkOrder(id="w").entity_type == "field_work_order"
    assert Appointment(id="a").entity_type == "field_appointment"
    assert Technician(id="t").entity_type == "field_technician"
    assert VisitOccurrence(id="v").entity_type == "field_visit_occurrence"


def test_no_access_is_a_reschedule_branch_not_history_rewrite():
    assert ("ready", "schedule", ("scheduled",)) in _edges(WorkOrderChart())
    assert ("in_progress", "require_reschedule", ("reschedule_required",)) in _edges(WorkOrderChart())
    assert ("reschedule_required", "reschedule", ("scheduled",)) in _edges(WorkOrderChart())
    assert ("in_progress", "no_access", ("no_access_recorded",)) in _edges(AppointmentChart())
    assert ("captured", "commit", ("committed",)) in _edges(VisitOccurrenceChart())


def test_technician_execution_ownership_is_separate():
    assert ("available", "assign", ("assigned",)) in _edges(TechnicianChart())
    assert ("assigned", "release", ("available",)) in _edges(TechnicianChart())


def test_field_service_transitions_are_orchestration_gated():
    for chart in (
        WorkOrderChart(),
        AppointmentChart(),
        TechnicianChart(),
        VisitOccurrenceChart(),
    ):
        assert policy_from_statechart(chart).events == ()

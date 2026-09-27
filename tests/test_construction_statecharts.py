from sose.examples.construction.entities import (
    ConstructionActivity,
    ConstructionInspection,
)
from sose.examples.construction.statecharts import ActivityChart, InspectionChart
from sose.statecharts.topology import graph_from_statechart, policy_from_statechart


def test_construction_entities_have_stable_types():
    assert ConstructionActivity(id="a").entity_type == "construction_activity"
    assert ConstructionInspection(id="i").entity_type == "construction_inspection"


def test_activity_chart_covers_dependency_material_execution_and_rework():
    graph = graph_from_statechart(ActivityChart())
    edges = {(edge.source, edge.event, edge.targets) for edge in graph.edges}

    assert ("planned", "block_dependency", ("blocked_dependency",)) in edges
    assert ("blocked_dependency", "dependency_ready", ("ready",)) in edges
    assert ("ready", "wait_material", ("waiting_material",)) in edges
    assert ("waiting_material", "material_ready", ("ready",)) in edges
    assert ("ready", "request_resources", ("waiting_resource",)) in edges
    assert ("waiting_resource", "start", ("executing",)) in edges
    assert ("executing", "finish_work", ("inspection",)) in edges
    assert ("inspection", "reject", ("rework",)) in edges
    assert ("rework", "schedule_rework", ("waiting_resource",)) in edges
    assert ("inspection", "accept", ("measured",)) in edges
    assert ("measured", "complete", ("completed",)) in edges


def test_inspection_outcome_is_immutable_occurrence():
    graph = graph_from_statechart(InspectionChart())
    edges = {(edge.source, edge.event, edge.targets) for edge in graph.edges}

    assert ("pending", "begin", ("inspecting",)) in edges
    assert ("inspecting", "pass_inspection", ("passed",)) in edges
    assert ("inspecting", "fail_inspection", ("failed",)) in edges
    assert not any(source in {"passed", "failed"} for source, _, _ in edges)


def test_construction_charts_are_orchestration_gated():
    for chart in (ActivityChart(), InspectionChart()):
        policy = policy_from_statechart(chart)
        assert policy.events == ()
        for edge in graph_from_statechart(chart).edges:
            if edge.event is not None:
                assert policy.is_probabilistically_eligible(edge.event) is False

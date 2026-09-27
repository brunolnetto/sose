from sose.examples.itsm.entities import Escalation, Incident
from sose.examples.itsm.statecharts import EscalationChart, IncidentChart
from sose.statecharts.topology import graph_from_statechart, policy_from_statechart


def test_itsm_entities_have_stable_types():
    assert Incident(id="incident-1").entity_type == "itsm_incident"
    assert Escalation(id="escalation-1").entity_type == "itsm_escalation"


def test_incident_chart_covers_assignment_escalation_resolution_and_reopen():
    graph = graph_from_statechart(IncidentChart())
    edges = {(edge.source, edge.event, edge.targets) for edge in graph.edges}

    assert ("opened", "triage", ("triaged",)) in edges
    assert ("triaged", "assign", ("assigned",)) in edges
    assert ("assigned", "start", ("in_progress",)) in edges
    assert ("triaged", "escalate", ("escalated",)) in edges
    assert ("assigned", "escalate", ("escalated",)) in edges
    assert ("in_progress", "escalate", ("escalated",)) in edges
    assert ("in_progress", "resolve", ("resolved",)) in edges
    assert ("escalated", "resolve", ("resolved",)) in edges
    assert ("resolved", "close", ("closed",)) in edges
    assert ("resolved", "reopen", ("in_progress",)) in edges


def test_incident_lifecycle_is_not_directly_probabilistic():
    policy = policy_from_statechart(IncidentChart())
    assert policy.events == ()
    for event in (
        "triage",
        "assign",
        "start",
        "resolve",
        "close",
        "escalate",
        "reopen",
    ):
        assert policy.is_probabilistically_eligible(event) is False


def test_escalation_is_an_independent_case_lifecycle():
    graph = graph_from_statechart(EscalationChart())
    policy = policy_from_statechart(EscalationChart())
    edges = {(edge.source, edge.event, edge.targets) for edge in graph.edges}

    assert ("raised", "acknowledge", ("acknowledged",)) in edges
    assert ("acknowledged", "take_ownership", ("owned",)) in edges
    assert ("owned", "mitigate", ("mitigated",)) in edges
    assert ("mitigated", "complete", ("completed",)) in edges
    assert policy.events == ()
    assert policy.is_probabilistically_eligible("acknowledge") is False

from sose.examples.hospitals.entities import Admission, TreatmentEpisode
from sose.examples.hospitals.statecharts import AdmissionChart, TreatmentEpisodeChart
from sose.statecharts.topology import graph_from_statechart, policy_from_statechart


def test_hospital_entities_have_stable_types():
    assert Admission(id="admission-1").entity_type == "hospital_admission"
    assert TreatmentEpisode(id="episode-1").entity_type == "hospital_treatment_episode"


def test_admission_chart_covers_bed_icu_discharge_and_transfer():
    graph = graph_from_statechart(AdmissionChart())
    edges = {(edge.source, edge.event, edge.targets) for edge in graph.edges}

    assert ("admitted", "triage", ("triaged",)) in edges
    assert ("triaged", "wait_bed", ("waiting_bed",)) in edges
    assert ("waiting_bed", "allocate_bed", ("bed_allocated",)) in edges
    assert ("bed_allocated", "start_treatment", ("treatment",)) in edges
    assert ("treatment", "deteriorate", ("waiting_icu",)) in edges
    assert ("waiting_icu", "allocate_icu", ("icu",)) in edges
    assert ("icu", "ready_discharge", ("discharge_ready",)) in edges
    assert ("discharge_ready", "discharge", ("discharged",)) in edges
    assert ("triaged", "transfer", ("transferred",)) in edges
    assert ("waiting_icu", "transfer", ("transferred",)) in edges
    assert ("waiting_bed", "transfer", ("transferred",)) not in edges


def test_treatment_episode_models_preemption_explicitly():
    graph = graph_from_statechart(TreatmentEpisodeChart())
    edges = {(edge.source, edge.event, edge.targets) for edge in graph.edges}

    assert ("planned", "queue", ("waiting_capacity",)) in edges
    assert ("waiting_capacity", "start", ("in_progress",)) in edges
    assert ("in_progress", "interrupt", ("interrupted",)) in edges
    assert ("interrupted", "resume", ("in_progress",)) in edges
    assert ("in_progress", "complete", ("completed",)) in edges


def test_hospital_lifecycles_are_orchestration_gated():
    for chart, events in (
        (
            AdmissionChart(),
            {
                "triage",
                "wait_bed",
                "allocate_bed",
                "start_treatment",
                "deteriorate",
                "allocate_icu",
                "ready_discharge",
                "discharge",
                "transfer",
            },
        ),
        (
            TreatmentEpisodeChart(),
            {"queue", "start", "interrupt", "resume", "complete", "cancel"},
        ),
    ):
        policy = policy_from_statechart(chart)
        assert policy.events == ()
        for event in events:
            assert policy.is_probabilistically_eligible(event) is False

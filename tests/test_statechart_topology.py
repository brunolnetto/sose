from sose.probability import probabilistic_transitions
from sose.statecharts.topology import graph_from_statechart, policy_from_statechart


class FakeEvent:
    def __init__(self, event_id):
        self.id = event_id


class FakeTransition:
    def __init__(self, source, targets, events=(), *, internal=False, initial=False):
        self.source = source
        self.targets = list(targets)
        self.events = list(events)
        self.internal = internal
        self.initial = initial


class FakeState:
    def __init__(self, state_id):
        self.id = state_id
        self.transitions = []
        self.states = []


@probabilistic_transitions({"start": 0.8, "wait": 0.2})
class FakeChart:
    def __init__(self):
        released = FakeState("released")
        in_progress = FakeState("in_progress")
        waiting = FakeState("waiting_material")
        auto = FakeState("auto_checked")

        released.transitions.extend(
            [
                FakeTransition(released, [in_progress], [FakeEvent("start")]),
                FakeTransition(released, [waiting], [FakeEvent("wait")]),
                FakeTransition(released, [auto], []),
            ]
        )
        self.states = [released, in_progress, waiting, auto]


def test_statechart_topology_is_extracted_without_duplicating_domain_graph():
    chart = FakeChart()
    graph = graph_from_statechart(chart)

    assert {(e.source, e.event, e.targets) for e in graph.edges} == {
        ("released", "start", ("in_progress",)),
        ("released", "wait", ("waiting_material",)),
        ("released", None, ("auto_checked",)),
    }


def test_probability_policy_can_be_embedded_on_statechart_class():
    policy = policy_from_statechart(FakeChart())
    assert set(policy.events) == {"start", "wait"}

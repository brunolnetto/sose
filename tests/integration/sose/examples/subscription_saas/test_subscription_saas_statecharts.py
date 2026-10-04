from sose.examples.subscription_saas.entities import (
    ChangeRequest,
    Entitlement,
    Subscription,
    SubscriptionOccurrence,
)
from sose.examples.subscription_saas.statecharts import (
    ChangeRequestChart,
    EntitlementChart,
    SubscriptionChart,
    SubscriptionOccurrenceChart,
)
from sose.statecharts.topology import graph_from_statechart, policy_from_statechart


def _edges(chart):
    graph = graph_from_statechart(chart)
    return {(edge.source, edge.event, edge.targets) for edge in graph.edges}


def test_subscription_entities_have_stable_types():
    assert Subscription(id="s").entity_type == "saas_subscription"
    assert Entitlement(id="e").entity_type == "saas_entitlement"
    assert ChangeRequest(id="c").entity_type == "saas_change_request"
    assert SubscriptionOccurrence(id="o").entity_type == "saas_subscription_occurrence"


def test_commercial_lifecycle_entitlement_and_change_intent_are_separate():
    assert ("active", "request_cancel", ("cancellation_pending",)) in _edges(SubscriptionChart())
    assert ("cancellation_pending", "end", ("ended",)) in _edges(SubscriptionChart())
    assert ("active", "supersede", ("superseded",)) in _edges(EntitlementChart())
    assert ("scheduled", "apply", ("applied",)) in _edges(ChangeRequestChart())
    assert ("captured", "commit", ("committed",)) in _edges(SubscriptionOccurrenceChart())


def test_subscription_transitions_are_orchestration_gated():
    for chart in (
        SubscriptionChart(),
        EntitlementChart(),
        ChangeRequestChart(),
        SubscriptionOccurrenceChart(),
    ):
        assert policy_from_statechart(chart).events == ()

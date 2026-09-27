from sose.examples.record_to_report.entities import (
    AccountingPeriod,
    Adjustment,
    CloseTask,
    JournalEntry,
    ReconciliationItem,
)
from sose.examples.record_to_report.statecharts import (
    AccountingPeriodChart,
    AdjustmentChart,
    CloseTaskChart,
    JournalEntryChart,
    ReconciliationItemChart,
)
from sose.statecharts.topology import graph_from_statechart, policy_from_statechart


def test_r2r_entities_have_stable_types():
    assert JournalEntry(id="j").entity_type == "journal_entry"
    assert ReconciliationItem(id="r").entity_type == "reconciliation_item"
    assert Adjustment(id="a").entity_type == "accounting_adjustment"
    assert CloseTask(id="c").entity_type == "close_task"
    assert AccountingPeriod(id="p").entity_type == "accounting_period"


def test_reconciliation_and_adjustment_topology():
    graph = graph_from_statechart(ReconciliationItemChart())
    edges = {(e.source, e.event, e.targets) for e in graph.edges}
    assert ("pending", "start", ("reconciling",)) in edges
    assert ("reconciling", "match", ("matched",)) in edges
    assert ("reconciling", "mark_unmatched", ("unmatched",)) in edges
    assert ("unmatched", "require_adjustment", ("adjustment_required",)) in edges
    assert ("adjustment_required", "apply_adjustment", ("reconciled",)) in edges

    adj = graph_from_statechart(AdjustmentChart())
    adj_edges = {(e.source, e.event, e.targets) for e in adj.edges}
    assert ("proposed", "submit", ("submitted",)) in adj_edges
    assert ("submitted", "approve", ("approved",)) in adj_edges
    assert ("approved", "post", ("posted",)) in adj_edges


def test_period_can_reopen_only_after_close():
    graph = graph_from_statechart(AccountingPeriodChart())
    edges = {(e.source, e.event, e.targets) for e in graph.edges}
    assert ("open", "prepare_close", ("close_ready",)) in edges
    assert ("close_ready", "close", ("closed",)) in edges
    assert ("closed", "reopen", ("reopened",)) in edges
    assert ("reopened", "prepare_close", ("close_ready",)) in edges


def test_r2r_lifecycles_are_orchestration_gated():
    for chart in (
        JournalEntryChart(),
        ReconciliationItemChart(),
        AdjustmentChart(),
        CloseTaskChart(),
        AccountingPeriodChart(),
    ):
        policy = policy_from_statechart(chart)
        assert policy.events == ()
        for edge in graph_from_statechart(chart).edges:
            if edge.event is not None:
                assert policy.is_probabilistically_eligible(edge.event) is False

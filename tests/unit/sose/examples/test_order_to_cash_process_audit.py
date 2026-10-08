from __future__ import annotations

from sose.examples.process_manifest import (
    PROCESS_MATURITY_REQUIREMENTS,
    ProcessEvidence,
    ProcessMaturity,
    audit_builtin_processes,
    builtin_process_manifests,
)


PC5_REQUIREMENTS = PROCESS_MATURITY_REQUIREMENTS[ProcessMaturity.PC5_OBSERVABLE]
PC6_GAPS = PROCESS_MATURITY_REQUIREMENTS[ProcessMaturity.PC6_COMPOSABLE]


def test_order_to_cash_audit_reaches_pc5_with_explicit_observability_evidence() -> None:
    manifest = builtin_process_manifests()["order_to_cash"]

    assert manifest.assessment_complete
    assert manifest.maturity is ProcessMaturity.PC5_OBSERVABLE
    assert not manifest.is_maturity_lower_bound
    assert manifest.is_complete_process_canonical
    assert not manifest.is_integrated_process_canonical
    assert manifest.trigger == "sales_order_submitted"
    assert manifest.terminal_outcomes == frozenset({"collected"})
    assert manifest.resources == frozenset({"fulfillment_team", "collection_agent"})
    assert manifest.sad_paths == frozenset(
        {
            "credit_hold",
            "fulfillment_capacity_loss",
            "fulfillment_contention",
            "partial_fulfillment",
            "overdue_collection",
        }
    )
    assert manifest.kpis == frozenset(
        {
            "cash_collection",
            "order_to_cash_seconds",
            "amount",
            "transition_count",
            "credit_hold_count",
            "partial_fulfillment_count",
            "overdue_count",
            "collection_case_count",
            "collection_escalation_count",
        }
    )
    assert manifest.missing_for(ProcessMaturity.PC5_OBSERVABLE) == frozenset()


def test_order_to_cash_pc5_claims_are_all_explicit() -> None:
    evidence = builtin_process_manifests()["order_to_cash"].evidence

    for claim in PC5_REQUIREMENTS:
        assert claim in evidence


def test_order_to_cash_pc5_contracts_are_backed_by_explicit_evidence() -> None:
    manifest = builtin_process_manifests()["order_to_cash"]

    for claim in PC5_REQUIREMENTS:
        assert claim in manifest.evidence
        assert manifest.evidence_sources[claim]


def test_order_to_cash_audit_reports_composition_as_next_gate() -> None:
    row = next(
        row for row in audit_builtin_processes() if row.domain == "order_to_cash"
    )

    assert row.next_maturity is ProcessMaturity.PC6_COMPOSABLE
    assert not row.assessment_is_lower_bound
    assert row.missing_for_next_gate == PC6_GAPS

from __future__ import annotations

from sose.examples.process_manifest import (
    PROCESS_MATURITY_REQUIREMENTS,
    ProcessEvidence,
    ProcessMaturity,
    audit_builtin_processes,
    builtin_process_manifests,
)


PC5_GAPS = PROCESS_MATURITY_REQUIREMENTS[ProcessMaturity.PC5_OBSERVABLE]


def test_order_to_cash_audit_reaches_pc4_without_inventing_pc5_evidence() -> None:
    manifest = builtin_process_manifests()["order_to_cash"]

    assert manifest.assessment_complete
    assert manifest.maturity is ProcessMaturity.PC4_DURABLE
    assert not manifest.is_maturity_lower_bound
    assert not manifest.is_complete_process_canonical
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
    assert manifest.kpis == frozenset()
    assert manifest.missing_for(ProcessMaturity.PC5_OBSERVABLE) == PC5_GAPS


def test_order_to_cash_pc4_claims_are_all_explicit() -> None:
    evidence = builtin_process_manifests()["order_to_cash"].evidence

    for claim in PROCESS_MATURITY_REQUIREMENTS[ProcessMaturity.PC4_DURABLE]:
        assert claim in evidence


def test_order_to_cash_does_not_credit_textual_specification_as_pc5_contracts() -> None:
    evidence = builtin_process_manifests()["order_to_cash"].evidence

    for claim in PROCESS_MATURITY_REQUIREMENTS[ProcessMaturity.PC5_OBSERVABLE]:
        assert claim not in evidence


def test_order_to_cash_audit_reports_observability_as_next_gate() -> None:
    row = next(
        row for row in audit_builtin_processes() if row.domain == "order_to_cash"
    )

    assert row.next_maturity is ProcessMaturity.PC5_OBSERVABLE
    assert not row.assessment_is_lower_bound
    assert row.missing_for_next_gate == PC5_GAPS

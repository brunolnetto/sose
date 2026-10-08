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


def test_p2p_audit_reaches_pc5_with_explicit_observability() -> None:
    manifest = builtin_process_manifests()["p2p"]

    assert manifest.assessment_complete
    assert manifest.maturity is ProcessMaturity.PC5_OBSERVABLE
    assert not manifest.is_maturity_lower_bound
    assert manifest.is_complete_process_canonical
    assert not manifest.is_integrated_process_canonical
    assert manifest.trigger == "requisition_requested"
    assert manifest.terminal_outcomes == frozenset({"consumed"})
    assert manifest.resources == frozenset({"receiving_dock", "inspector"})
    assert manifest.sad_paths == frozenset(
        {
            "receiving_contention",
            "shortage_backorder",
            "partial_receipt",
            "rejected_receipt",
        }
    )
    assert manifest.kpis == frozenset(
        {
            "procure_to_consumption_seconds",
            "quantity",
            "transition_count",
            "supplier_delay_count",
            "partial_receipt_count",
            "rejected_receipt_count",
            "backorder_count",
            "consumed",
        }
    )
    assert manifest.missing_for(ProcessMaturity.PC5_OBSERVABLE) == frozenset()


def test_p2p_pc5_claims_are_explicit_and_have_provenance() -> None:
    manifest = builtin_process_manifests()["p2p"]

    for claim in PC5_REQUIREMENTS:
        assert claim in manifest.evidence
        assert manifest.evidence_sources[claim]


def test_p2p_audit_reports_composition_as_next_gate() -> None:
    row = next(row for row in audit_builtin_processes() if row.domain == "p2p")

    assert row.next_maturity is ProcessMaturity.PC6_COMPOSABLE
    assert not row.assessment_is_lower_bound
    assert row.missing_for_next_gate == PC6_GAPS

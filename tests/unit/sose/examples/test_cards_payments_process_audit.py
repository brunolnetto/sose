from __future__ import annotations

from pathlib import Path

from sose.examples.process_manifest import (
    PROCESS_MATURITY_REQUIREMENTS,
    ProcessMaturity,
    audit_builtin_processes,
    builtin_process_manifests,
)


REPO_ROOT = Path(__file__).resolve().parents[4]
PC5_REQUIREMENTS = PROCESS_MATURITY_REQUIREMENTS[ProcessMaturity.PC5_OBSERVABLE]
PC6_GAPS = PROCESS_MATURITY_REQUIREMENTS[ProcessMaturity.PC6_COMPOSABLE]


def test_cards_payments_audit_reaches_pc5_with_explicit_observability() -> None:
    manifest = builtin_process_manifests()["cards_payments"]

    assert manifest.assessment_complete
    assert manifest.maturity is ProcessMaturity.PC5_OBSERVABLE
    assert not manifest.is_maturity_lower_bound
    assert manifest.is_complete_process_canonical
    assert not manifest.is_integrated_process_canonical
    assert manifest.trigger == "payment_authorization_requested"
    assert manifest.terminal_outcomes == frozenset(
        {"declined", "reversed", "refunded"}
    )
    assert manifest.resources == frozenset(
        {"authorization_processor", "settlement_processor", "dispute_analyst"}
    )
    assert manifest.sad_paths == frozenset(
        {
            "authorization_decline",
            "authorization_reversal",
            "settlement_retry",
            "processor_outage",
            "post_settlement_refund",
            "dispute_chargeback",
        }
    )
    assert manifest.missing_for(ProcessMaturity.PC5_OBSERVABLE) == frozenset()


def test_cards_payments_pc5_claims_have_direct_provenance() -> None:
    manifest = builtin_process_manifests()["cards_payments"]

    for evidence in PROCESS_MATURITY_REQUIREMENTS[ProcessMaturity.PC4_DURABLE]:
        paths = manifest.evidence_sources[evidence]
        assert paths
        for relative in paths:
            assert (REPO_ROOT / relative).is_file(), (evidence, relative)

    for evidence in PC5_REQUIREMENTS:
        assert evidence in manifest.evidence
        paths = manifest.evidence_sources[evidence]
        assert paths
        for relative in paths:
            assert (REPO_ROOT / relative).is_file(), (evidence, relative)


def test_cards_payments_audit_reports_composition_as_next_gate() -> None:
    row = next(
        row for row in audit_builtin_processes()
        if row.domain == "cards_payments"
    )

    assert row.next_maturity is ProcessMaturity.PC6_COMPOSABLE
    assert not row.assessment_is_lower_bound
    assert row.missing_for_next_gate == PC6_GAPS

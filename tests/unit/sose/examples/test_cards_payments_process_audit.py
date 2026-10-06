from __future__ import annotations

from pathlib import Path

from sose.examples.process_manifest import (
    PROCESS_MATURITY_REQUIREMENTS,
    ProcessEvidence,
    ProcessMaturity,
    builtin_process_manifests,
)


REPO_ROOT = Path(__file__).resolve().parents[4]


def test_cards_payments_audit_reaches_pc4_without_overclaiming_pc5() -> None:
    manifest = builtin_process_manifests()["cards_payments"]

    assert manifest.assessment_complete
    assert manifest.maturity is ProcessMaturity.PC4_DURABLE
    assert not manifest.is_maturity_lower_bound
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

    assert manifest.missing_for(ProcessMaturity.PC5_OBSERVABLE) == frozenset(
        {
            ProcessEvidence.KPIS,
            ProcessEvidence.PROCESS_DIAGRAM,
            ProcessEvidence.PROJECTION_CONTRACT,
            ProcessEvidence.CONFIGURATION_DOCUMENTATION,
        }
    )
    assert ProcessEvidence.ERD in manifest.evidence
    assert ProcessEvidence.STATECHART_DOCUMENTATION in manifest.evidence


def test_cards_payments_pc4_claims_have_executable_provenance() -> None:
    manifest = builtin_process_manifests()["cards_payments"]
    pc4 = PROCESS_MATURITY_REQUIREMENTS[ProcessMaturity.PC4_DURABLE]

    assert pc4 <= manifest.evidence
    for evidence in pc4:
        paths = manifest.evidence_sources[evidence]
        assert paths
        for relative in paths:
            assert (REPO_ROOT / relative).is_file(), (evidence, relative)


def test_cards_payments_restart_replay_and_fault_recovery_are_directly_proven() -> None:
    manifest = builtin_process_manifests()["cards_payments"]

    assert any(
        path.startswith("tests/e2e/")
        for path in manifest.evidence_sources[ProcessEvidence.RESTART_EQUIVALENCE]
    )
    assert any(
        "simulation_edges" in path
        for path in manifest.evidence_sources[ProcessEvidence.REPLAY_IDEMPOTENCE]
    )
    assert any(
        "scenarios" in path
        for path in manifest.evidence_sources[ProcessEvidence.FAULT_RECOVERY]
    )

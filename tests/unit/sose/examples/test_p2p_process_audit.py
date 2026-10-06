from __future__ import annotations

from sose.examples.process_manifest import (
    ProcessEvidence,
    ProcessMaturity,
    audit_builtin_processes,
    builtin_process_manifests,
)


def test_p2p_audit_reaches_pc4_without_overclaiming_observability() -> None:
    manifest = builtin_process_manifests()["p2p"]

    assert manifest.assessment_complete
    assert manifest.maturity is ProcessMaturity.PC4_DURABLE
    assert not manifest.is_maturity_lower_bound
    assert not manifest.is_complete_process_canonical
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
    assert manifest.kpis == frozenset()

    assert manifest.missing_for(ProcessMaturity.PC5_OBSERVABLE) == frozenset(
        {
            ProcessEvidence.KPIS,
            ProcessEvidence.PROJECTION_CONTRACT,
            ProcessEvidence.CONFIGURATION_DOCUMENTATION,
        }
    )


def test_p2p_pc4_claims_are_explicitly_present() -> None:
    evidence = builtin_process_manifests()["p2p"].evidence

    for claim in {
        ProcessEvidence.DURABLE_STATE,
        ProcessEvidence.RESTART_EQUIVALENCE,
        ProcessEvidence.REPLAY_IDEMPOTENCE,
        ProcessEvidence.RECURRING_RECONCILIATION,
        ProcessEvidence.FAULT_RECOVERY,
    }:
        assert claim in evidence


def test_p2p_existing_documentation_supports_only_three_pc5_claims() -> None:
    evidence = builtin_process_manifests()["p2p"].evidence

    assert ProcessEvidence.ERD in evidence
    assert ProcessEvidence.STATECHART_DOCUMENTATION in evidence
    assert ProcessEvidence.PROCESS_DIAGRAM in evidence

    assert ProcessEvidence.KPIS not in evidence
    assert ProcessEvidence.PROJECTION_CONTRACT not in evidence
    assert ProcessEvidence.CONFIGURATION_DOCUMENTATION not in evidence


def test_p2p_audit_reports_observability_as_next_gate() -> None:
    row = next(row for row in audit_builtin_processes() if row.domain == "p2p")

    assert row.next_maturity is ProcessMaturity.PC5_OBSERVABLE
    assert not row.assessment_is_lower_bound
    assert row.missing_for_next_gate == frozenset(
        {
            ProcessEvidence.KPIS,
            ProcessEvidence.PROJECTION_CONTRACT,
            ProcessEvidence.CONFIGURATION_DOCUMENTATION,
        }
    )

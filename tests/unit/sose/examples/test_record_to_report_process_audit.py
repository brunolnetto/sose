from __future__ import annotations

from pathlib import Path

from sose.examples.process_manifest import (
    PROCESS_MATURITY_REQUIREMENTS,
    ProcessEvidence,
    ProcessMaturity,
    builtin_process_manifests,
)


REPO_ROOT = Path(__file__).resolve().parents[4]


def test_record_to_report_audit_reaches_pc4_without_overclaiming_pc5() -> None:
    manifest = builtin_process_manifests()["record_to_report"]

    assert manifest.assessment_complete
    assert manifest.maturity is ProcessMaturity.PC4_DURABLE
    assert not manifest.is_maturity_lower_bound
    assert manifest.trigger == "journal_drafted"
    assert manifest.terminal_outcomes == frozenset({"close_task_completed"})
    assert manifest.resources == frozenset(
        {"posting_processor", "reconciliation_analyst", "close_accountant"}
    )
    assert manifest.sad_paths == frozenset(
        {
            "rejected_posting",
            "unmatched_adjustment",
            "posting_outage",
            "close_accountant_contention",
            "close_team_shortage",
            "controlled_reopen",
        }
    )

    assert manifest.missing_for(ProcessMaturity.PC5_OBSERVABLE) == frozenset(
        {
            ProcessEvidence.KPIS,
            ProcessEvidence.ERD,
            ProcessEvidence.PROCESS_DIAGRAM,
            ProcessEvidence.PROJECTION_CONTRACT,
            ProcessEvidence.CONFIGURATION_DOCUMENTATION,
        }
    )
    assert ProcessEvidence.STATECHART_DOCUMENTATION in manifest.evidence


def test_record_to_report_pc4_claims_have_executable_provenance() -> None:
    manifest = builtin_process_manifests()["record_to_report"]
    pc4 = PROCESS_MATURITY_REQUIREMENTS[ProcessMaturity.PC4_DURABLE]

    assert pc4 <= manifest.evidence
    for evidence in pc4:
        paths = manifest.evidence_sources[evidence]
        assert paths
        for relative in paths:
            assert (REPO_ROOT / relative).is_file(), (evidence, relative)


def test_record_to_report_restart_replay_and_fault_recovery_are_directly_proven() -> None:
    manifest = builtin_process_manifests()["record_to_report"]

    restart_sources = manifest.evidence_sources[ProcessEvidence.RESTART_EQUIVALENCE]
    replay_sources = manifest.evidence_sources[ProcessEvidence.REPLAY_IDEMPOTENCE]
    recovery_sources = manifest.evidence_sources[ProcessEvidence.FAULT_RECOVERY]

    assert any(path.startswith("tests/e2e/") for path in restart_sources)
    assert any("coverage_record_to_report_edges" in path for path in replay_sources)
    assert any("scenarios" in path for path in recovery_sources)

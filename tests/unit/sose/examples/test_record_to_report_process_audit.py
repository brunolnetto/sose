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


def test_record_to_report_audit_reaches_pc5_with_explicit_observability() -> None:
    manifest = builtin_process_manifests()["record_to_report"]

    assert manifest.assessment_complete
    assert manifest.maturity is ProcessMaturity.PC5_OBSERVABLE
    assert not manifest.is_maturity_lower_bound
    assert manifest.is_complete_process_canonical
    assert not manifest.is_integrated_process_canonical
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
    assert manifest.missing_for(ProcessMaturity.PC5_OBSERVABLE) == frozenset()


def test_record_to_report_pc5_claims_have_direct_provenance() -> None:
    manifest = builtin_process_manifests()["record_to_report"]

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


def test_record_to_report_audit_reports_composition_as_next_gate() -> None:
    row = next(
        row for row in audit_builtin_processes()
        if row.domain == "record_to_report"
    )

    assert row.next_maturity is ProcessMaturity.PC6_COMPOSABLE
    assert not row.assessment_is_lower_bound
    assert row.missing_for_next_gate == PC6_GAPS

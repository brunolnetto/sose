from __future__ import annotations

from pathlib import Path

from sose.examples.catalog import builtin_catalog
from sose.examples.process_manifest import (
    PROCESS_AUDIT_EXCLUDED_DOMAINS,
    PROCESS_MATURITY_REQUIREMENTS,
    ProcessEvidence,
    ProcessMaturity,
    audit_builtin_processes,
    builtin_process_manifests,
)


REPO_ROOT = Path(__file__).resolve().parents[4]


def test_every_builtin_business_domain_has_an_auditable_manifest_entry() -> None:
    catalog_domains = set(builtin_catalog().names(kind="domain"))
    business_domains = catalog_domains - PROCESS_AUDIT_EXCLUDED_DOMAINS
    manifests = builtin_process_manifests()

    assert set(manifests) == business_domains
    assert "tutorial_job" not in manifests


def test_process_audit_exclusions_are_explicit_catalog_domains() -> None:
    catalog_domains = set(builtin_catalog().names(kind="domain"))

    assert PROCESS_AUDIT_EXCLUDED_DOMAINS == frozenset({"tutorial_job"})
    assert PROCESS_AUDIT_EXCLUDED_DOMAINS <= catalog_domains


def test_warehouse_fulfillment_audit_stops_at_pc2_and_preserves_audited_higher_evidence() -> None:
    manifest = builtin_process_manifests()["warehouse_fulfillment"]

    assert manifest.assessment_complete
    assert manifest.maturity is ProcessMaturity.PC2_PROCESS
    assert not manifest.is_maturity_lower_bound
    assert manifest.trigger == "fulfillment_order_requested"
    assert manifest.terminal_outcomes == frozenset({"shipped"})
    assert manifest.resources == frozenset()
    assert manifest.kpis == frozenset()
    assert manifest.sad_paths == frozenset(
        {
            "insufficient_inventory",
            "pack_before_all_allocations_picked",
            "conflicting_correction_replay",
            "correction_below_allocated_quantity",
        }
    )

    assert manifest.missing_for(ProcessMaturity.PC3_OPERATIONAL) == frozenset(
        {
            ProcessEvidence.FINITE_RESOURCES,
            ProcessEvidence.CAPACITY_CONTENTION,
            ProcessEvidence.TIME_SEMANTICS,
        }
    )

    assert ProcessEvidence.DURABLE_STATE in manifest.evidence
    assert ProcessEvidence.REPLAY_IDEMPOTENCE in manifest.evidence
    assert ProcessEvidence.RECURRING_RECONCILIATION in manifest.evidence

    # Recovery after rebuild is useful evidence, but the current test does not
    # compare a continuous baseline with a rebuilt execution. Do not overclaim it.
    assert ProcessEvidence.RESTART_EQUIVALENCE not in manifest.evidence
    # The prose lifecycle summary omits real cancellation transitions, so it is
    # not yet complete statechart documentation under the PC5 contract.
    assert ProcessEvidence.STATECHART_DOCUMENTATION not in manifest.evidence

    assert ProcessEvidence.FAULT_RECOVERY not in manifest.evidence
    assert ProcessEvidence.KPIS not in manifest.evidence
    assert ProcessEvidence.ERD not in manifest.evidence
    assert ProcessEvidence.PROCESS_DIAGRAM not in manifest.evidence
    assert ProcessEvidence.PROJECTION_CONTRACT not in manifest.evidence
    assert ProcessEvidence.CONFIGURATION_DOCUMENTATION not in manifest.evidence


def test_warehouse_management_audit_exposes_restart_equivalence_as_pc4_gap() -> None:
    manifest = builtin_process_manifests()["warehouse_management"]

    assert manifest.assessment_complete
    assert manifest.maturity is ProcessMaturity.PC3_OPERATIONAL
    assert not manifest.is_complete_process_canonical
    assert not manifest.is_integrated_process_canonical
    assert "dock" in manifest.resources
    assert "stock" not in manifest.resources
    assert "on_time" in manifest.kpis
    assert ProcessEvidence.RESTART_EQUIVALENCE not in manifest.evidence
    assert ProcessEvidence.FAULT_RECOVERY in manifest.evidence
    assert ProcessEvidence.KPIS in manifest.evidence


def test_every_audited_process_claim_has_provenance_and_existing_sources() -> None:
    pc0 = PROCESS_MATURITY_REQUIREMENTS[ProcessMaturity.PC0_REGISTERED]

    for manifest in builtin_process_manifests().values():
        if not manifest.assessment_complete:
            continue
        assert set(manifest.evidence_sources) == set(manifest.evidence - pc0)
        for evidence, paths in manifest.evidence_sources.items():
            assert evidence in manifest.evidence
            assert paths
            for relative in paths:
                assert (REPO_ROOT / relative).is_file(), (
                    manifest.domain,
                    evidence,
                    relative,
                )


def test_audit_orders_domains_by_maturity_then_name_and_reports_next_gate() -> None:
    audit = audit_builtin_processes()

    assert audit[0].maturity >= audit[-1].maturity

    management = next(row for row in audit if row.domain == "warehouse_management")
    assert management.next_maturity is ProcessMaturity.PC4_DURABLE
    assert not management.assessment_is_lower_bound
    assert management.missing_for_next_gate == frozenset(
        {ProcessEvidence.RESTART_EQUIVALENCE}
    )

    fulfillment = next(row for row in audit if row.domain == "warehouse_fulfillment")
    assert fulfillment.next_maturity is ProcessMaturity.PC3_OPERATIONAL
    assert not fulfillment.assessment_is_lower_bound
    assert fulfillment.missing_for_next_gate == frozenset(
        {
            ProcessEvidence.FINITE_RESOURCES,
            ProcessEvidence.CAPACITY_CONTENTION,
            ProcessEvidence.TIME_SEMANTICS,
        }
    )

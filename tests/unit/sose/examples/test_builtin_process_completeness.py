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


def test_unreviewed_domains_are_explicit_lower_bounds_not_false_gap_claims() -> None:
    manifests = builtin_process_manifests()
    fulfillment = manifests["warehouse_fulfillment"]

    assert fulfillment.maturity is ProcessMaturity.PC0_REGISTERED
    assert not fulfillment.assessment_complete
    assert fulfillment.is_maturity_lower_bound
    assert not fulfillment.evidence_sources


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


def test_every_audited_warehouse_claim_has_provenance_and_existing_sources() -> None:
    manifest = builtin_process_manifests()["warehouse_management"]
    pc0 = PROCESS_MATURITY_REQUIREMENTS[ProcessMaturity.PC0_REGISTERED]

    assert set(manifest.evidence_sources) == set(manifest.evidence - pc0)
    for evidence, paths in manifest.evidence_sources.items():
        assert evidence in manifest.evidence
        assert paths
        for relative in paths:
            assert (REPO_ROOT / relative).is_file(), (evidence, relative)


def test_audit_orders_domains_by_maturity_then_name_and_reports_next_gate() -> None:
    audit = audit_builtin_processes()

    assert audit[0].maturity >= audit[-1].maturity
    warehouse = next(row for row in audit if row.domain == "warehouse_management")
    assert warehouse.next_maturity is ProcessMaturity.PC4_DURABLE
    assert not warehouse.assessment_is_lower_bound
    assert warehouse.missing_for_next_gate == frozenset(
        {ProcessEvidence.RESTART_EQUIVALENCE}
    )

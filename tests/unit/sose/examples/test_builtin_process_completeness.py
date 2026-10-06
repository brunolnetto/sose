from __future__ import annotations

from sose.examples.catalog import builtin_catalog
from sose.examples.process_manifest import (
    ProcessEvidence,
    ProcessMaturity,
    audit_builtin_processes,
    builtin_process_manifests,
)


def test_every_builtin_business_domain_has_an_auditable_manifest_entry() -> None:
    catalog_domains = set(builtin_catalog().names(kind="domain"))
    manifests = builtin_process_manifests()

    assert set(manifests) == catalog_domains


def test_unreviewed_domains_are_explicit_lower_bounds_not_false_gap_claims() -> None:
    manifests = builtin_process_manifests()
    fulfillment = manifests["warehouse_fulfillment"]

    assert fulfillment.maturity is ProcessMaturity.PC0_REGISTERED
    assert not fulfillment.assessment_complete
    assert fulfillment.is_maturity_lower_bound


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


def test_audit_orders_domains_by_maturity_then_name_and_reports_next_gate() -> None:
    audit = audit_builtin_processes()

    assert audit[0].maturity >= audit[-1].maturity
    warehouse = next(row for row in audit if row.domain == "warehouse_management")
    assert warehouse.next_maturity is ProcessMaturity.PC4_DURABLE
    assert not warehouse.assessment_is_lower_bound
    assert warehouse.missing_for_next_gate == frozenset(
        {ProcessEvidence.RESTART_EQUIVALENCE}
    )

"""PC6 promotion depends on executable typed ingress and durable egress."""
from __future__ import annotations

from pathlib import Path

from sose.examples.process_manifest import (
    ProcessEvidence,
    ProcessMaturity,
    builtin_process_manifests,
)


ROOT = Path(__file__).resolve().parents[4]


def test_warehouse_fulfillment_pc6_has_named_directional_contracts():
    manifest = builtin_process_manifests()["warehouse_fulfillment"]
    assert manifest.assessment_complete
    assert manifest.maturity is ProcessMaturity.PC6_COMPOSABLE
    assert manifest.is_integrated_process_canonical
    assert manifest.missing_for(ProcessMaturity.PC6_COMPOSABLE) == frozenset()

    assert manifest.ingress_contracts == frozenset({
        "o2c.fulfillment_requested.v1",
        "warehouse.inventory_reserved.v1",
    })
    assert manifest.egress_contracts == frozenset({
        "warehouse.inventory_reservation_requested.v1",
        "warehouse.inventory_consumption_requested.v1",
        "warehouse.dispatch_ready.v1",
    })


def test_pc6_provenance_references_executable_recovery_and_ownership_tests():
    manifest = builtin_process_manifests()["warehouse_fulfillment"]
    requirements = {
        ProcessEvidence.INGRESS_CONTRACTS:
            "test_trading_company_payload_driven_ingress.py",
        ProcessEvidence.EGRESS_CONTRACTS:
            "test_trading_company_shipped_egress_recovery.py",
        ProcessEvidence.CROSS_DOMAIN_EXECUTION:
            "test_trading_company_recovery_conformance.py",
    }
    for evidence, required_test in requirements.items():
        paths = manifest.evidence_sources[evidence]
        assert paths
        assert any(path.endswith(required_test) for path in paths)
        assert all((ROOT / path).is_file() for path in paths)

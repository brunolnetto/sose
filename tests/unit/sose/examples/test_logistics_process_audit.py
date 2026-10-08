from __future__ import annotations

from pathlib import Path

from sose.examples.process_manifest import (
    PROCESS_MATURITY_REQUIREMENTS,
    ProcessEvidence,
    ProcessMaturity,
    audit_builtin_processes,
    builtin_process_manifests,
)


REPO_ROOT = Path(__file__).resolve().parents[4]
PC5_REQUIREMENTS = PROCESS_MATURITY_REQUIREMENTS[ProcessMaturity.PC5_OBSERVABLE]
PC6_GAPS = PROCESS_MATURITY_REQUIREMENTS[ProcessMaturity.PC6_COMPOSABLE]


def test_logistics_audit_reaches_pc5_with_explicit_observability() -> None:
    manifest = builtin_process_manifests()["logistics"]

    assert manifest.assessment_complete
    assert manifest.maturity is ProcessMaturity.PC5_OBSERVABLE
    assert not manifest.is_maturity_lower_bound
    assert manifest.is_complete_process_canonical
    assert not manifest.is_integrated_process_canonical
    assert manifest.trigger == "shipment_created"
    assert manifest.terminal_outcomes == frozenset(
        {"delivered", "lost", "damaged", "returned"}
    )
    assert manifest.resources == frozenset(
        {
            "pickup_courier",
            "origin_dock",
            "transfer_vehicle",
            "destination_dock",
            "delivery_courier",
        }
    )
    assert manifest.sad_paths == frozenset(
        {
            "hub_congestion",
            "failed_delivery_retry",
            "courier_capacity_loss",
            "weather_delay",
            "lost_damaged_returned",
        }
    )
    assert manifest.kpis == frozenset(
        {
            "shipment_lead_time_seconds",
            "transition_count",
            "delay_count",
            "delivery_attempt_count",
            "failed_attempt_count",
            "delivered_attempt_count",
            "delivered",
        }
    )
    assert manifest.missing_for(ProcessMaturity.PC5_OBSERVABLE) == frozenset()


def test_logistics_pc5_claims_have_direct_provenance() -> None:
    manifest = builtin_process_manifests()["logistics"]

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


def test_logistics_audit_reports_composition_as_next_gate() -> None:
    row = next(row for row in audit_builtin_processes() if row.domain == "logistics")

    assert row.next_maturity is ProcessMaturity.PC6_COMPOSABLE
    assert not row.assessment_is_lower_bound
    assert row.missing_for_next_gate == PC6_GAPS

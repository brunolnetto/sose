from __future__ import annotations

from pathlib import Path

from sose.examples.process_manifest import (
    PROCESS_MATURITY_REQUIREMENTS,
    ProcessEvidence,
    ProcessMaturity,
    builtin_process_manifests,
)


REPO_ROOT = Path(__file__).resolve().parents[4]


def test_logistics_audit_reaches_pc4_without_overclaiming_pc5() -> None:
    manifest = builtin_process_manifests()["logistics"]

    assert manifest.assessment_complete
    assert manifest.maturity is ProcessMaturity.PC4_DURABLE
    assert not manifest.is_maturity_lower_bound
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


def test_logistics_pc4_claims_have_direct_executable_provenance() -> None:
    manifest = builtin_process_manifests()["logistics"]
    pc4 = PROCESS_MATURITY_REQUIREMENTS[ProcessMaturity.PC4_DURABLE]

    assert pc4 <= manifest.evidence
    for evidence in pc4:
        paths = manifest.evidence_sources[evidence]
        assert paths
        for relative in paths:
            assert (REPO_ROOT / relative).is_file(), (evidence, relative)


def test_logistics_restart_and_replay_claims_are_not_inferred_from_docs_only() -> None:
    manifest = builtin_process_manifests()["logistics"]

    restart_sources = manifest.evidence_sources[ProcessEvidence.RESTART_EQUIVALENCE]
    replay_sources = manifest.evidence_sources[ProcessEvidence.REPLAY_IDEMPOTENCE]

    assert any(path.startswith("tests/e2e/") for path in restart_sources)
    assert any(path.startswith("tests/unit/") for path in replay_sources)
    assert all(not path.endswith("specification.md") for path in replay_sources)

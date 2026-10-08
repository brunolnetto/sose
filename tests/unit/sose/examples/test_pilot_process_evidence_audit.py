from __future__ import annotations

from sose.examples.process_manifest import (
    ProcessEvidence,
    ProcessMaturity,
    builtin_process_manifests,
)


def test_multidomain_pilot_process_audits_are_complete_at_pc4() -> None:
    manifests = builtin_process_manifests()

    for domain in ("manufacturing", "mro", "order_to_cash"):
        manifest = manifests[domain]
        assert manifest.assessment_complete is True
        assert manifest.maturity is ProcessMaturity.PC4_DURABLE
        assert not manifest.is_maturity_lower_bound
        assert not manifest.is_complete_process_canonical


def test_manufacturing_pc5_gap_is_observability_contract_only() -> None:
    manifest = builtin_process_manifests()["manufacturing"]

    assert manifest.missing_for(ProcessMaturity.PC5_OBSERVABLE) == frozenset(
        {
            ProcessEvidence.KPIS,
            ProcessEvidence.PROJECTION_CONTRACT,
            ProcessEvidence.CONFIGURATION_DOCUMENTATION,
        }
    )


def test_mro_pc5_gap_is_observability_contract_only() -> None:
    manifest = builtin_process_manifests()["mro"]

    assert manifest.missing_for(ProcessMaturity.PC5_OBSERVABLE) == frozenset(
        {
            ProcessEvidence.KPIS,
            ProcessEvidence.PROJECTION_CONTRACT,
            ProcessEvidence.CONFIGURATION_DOCUMENTATION,
        }
    )


def test_o2c_pc5_gap_is_explicit_after_existing_audit() -> None:
    manifest = builtin_process_manifests()["order_to_cash"]

    assert manifest.missing_for(ProcessMaturity.PC5_OBSERVABLE) == frozenset(
        {
            ProcessEvidence.KPIS,
            ProcessEvidence.PROCESS_DIAGRAM,
            ProcessEvidence.PROJECTION_CONTRACT,
            ProcessEvidence.CONFIGURATION_DOCUMENTATION,
        }
    )


def test_pilot_audits_bind_every_nonbaseline_claim_to_provenance() -> None:
    manifests = builtin_process_manifests()
    baseline = {
        ProcessEvidence.REGISTERED,
        ProcessEvidence.CONFIGURABLE_RUNTIME,
    }

    for domain in ("manufacturing", "mro", "order_to_cash"):
        manifest = manifests[domain]
        assert set(manifest.evidence_sources) == set(manifest.evidence) - baseline
        assert all(manifest.evidence_sources[item] for item in manifest.evidence - baseline)

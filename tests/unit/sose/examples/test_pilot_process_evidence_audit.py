from __future__ import annotations

from sose.examples.process_manifest import (
    ProcessEvidence,
    ProcessMaturity,
    builtin_process_manifests,
)


def test_all_multidomain_pilot_process_audits_are_complete_at_pc5() -> None:
    manifests = builtin_process_manifests()

    for domain in ("manufacturing", "mro", "order_to_cash"):
        manifest = manifests[domain]
        assert manifest.assessment_complete is True
        assert manifest.maturity is ProcessMaturity.PC5_OBSERVABLE
        assert not manifest.is_maturity_lower_bound
        assert manifest.is_complete_process_canonical
        assert manifest.missing_for(ProcessMaturity.PC5_OBSERVABLE) == frozenset()


def test_manufacturing_is_promoted_to_pc5_with_complete_observability_evidence() -> None:
    manifest = builtin_process_manifests()["manufacturing"]

    assert manifest.assessment_complete is True
    assert manifest.maturity is ProcessMaturity.PC5_OBSERVABLE
    assert not manifest.is_maturity_lower_bound
    assert manifest.is_complete_process_canonical
    assert manifest.missing_for(ProcessMaturity.PC5_OBSERVABLE) == frozenset()


def test_mro_is_promoted_to_pc5_with_complete_observability_evidence() -> None:
    manifest = builtin_process_manifests()["mro"]

    assert manifest.assessment_complete is True
    assert manifest.maturity is ProcessMaturity.PC5_OBSERVABLE
    assert not manifest.is_maturity_lower_bound
    assert manifest.is_complete_process_canonical
    assert manifest.missing_for(ProcessMaturity.PC5_OBSERVABLE) == frozenset()


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

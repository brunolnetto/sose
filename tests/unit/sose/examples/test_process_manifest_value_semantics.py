from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict

from sose.examples.process_manifest import (
    PROCESS_MATURITY_REQUIREMENTS,
    ProcessEvidence,
    ProcessManifest,
    ProcessMaturity,
)


def test_registered_manifest_remains_hashable_and_deepcopy_serializable() -> None:
    manifest = ProcessManifest(
        domain="example",
        evidence=PROCESS_MATURITY_REQUIREMENTS[ProcessMaturity.PC0_REGISTERED],
    )

    assert isinstance(hash(manifest), int)
    assert deepcopy(manifest) == manifest
    payload = asdict(manifest)
    assert payload["domain"] == "example"
    assert "evidence_sources" in payload


def test_audited_manifest_with_provenance_remains_hashable_and_copyable() -> None:
    evidence = (
        PROCESS_MATURITY_REQUIREMENTS[ProcessMaturity.PC0_REGISTERED]
        | PROCESS_MATURITY_REQUIREMENTS[ProcessMaturity.PC1_BEHAVIORAL]
    )
    sources = {
        item: (f"tests/evidence/{item.value}.py",)
        for item in evidence
        if item
        not in PROCESS_MATURITY_REQUIREMENTS[ProcessMaturity.PC0_REGISTERED]
    }
    manifest = ProcessManifest(
        domain="example",
        evidence=evidence,
        trigger="request_created",
        evidence_sources=sources,
        assessment_complete=True,
    )

    assert isinstance(hash(manifest), int)
    clone = deepcopy(manifest)
    assert clone == manifest
    assert clone.evidence_sources[ProcessEvidence.HAPPY_PATH] == (
        "tests/evidence/happy_path.py",
    )
    assert "evidence_sources" in asdict(manifest)

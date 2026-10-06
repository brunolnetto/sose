from __future__ import annotations

from sose.examples.process_manifest import (
    PROCESS_MATURITY_REQUIREMENTS,
    ProcessEvidence,
    ProcessManifest,
    ProcessMaturity,
)


def _evidence_through(level: ProcessMaturity) -> frozenset[ProcessEvidence]:
    evidence: set[ProcessEvidence] = set()
    for maturity in ProcessMaturity:
        if maturity > level:
            break
        evidence.update(PROCESS_MATURITY_REQUIREMENTS[maturity])
    return frozenset(evidence)


def _sources_for(
    evidence: frozenset[ProcessEvidence],
) -> dict[ProcessEvidence, tuple[str, ...]]:
    pc0 = PROCESS_MATURITY_REQUIREMENTS[ProcessMaturity.PC0_REGISTERED]
    return {
        item: (f"tests/evidence/{item.value}.py",)
        for item in evidence - pc0
    }


def _manifest(level: ProcessMaturity) -> ProcessManifest:
    return ProcessManifest(
        domain="example",
        evidence=_evidence_through(level),
        trigger="request_created",
        terminal_outcomes=frozenset({"completed", "cancelled"}),
        resources=frozenset({"worker"}),
        sad_paths=frozenset({"rejected"}),
        kpis=frozenset({"lead_time"}),
        specification_path="src/sose/examples/example/specification.md",
        ingress_contracts=frozenset({"RequestAccepted"}),
        egress_contracts=frozenset({"WorkCompleted"}),
    )


def test_manifest_requires_registered_runtime_baseline() -> None:
    try:
        ProcessManifest(domain="example", evidence=frozenset())
    except ValueError as exc:
        message = str(exc)
    else:  # pragma: no cover - contract guard
        raise AssertionError("manifest accepted evidence below PC0")

    assert "PC0" in message


def test_maturity_is_derived_from_cumulative_evidence() -> None:
    for level in ProcessMaturity:
        assert _manifest(level).maturity is level


def test_missing_for_next_level_reports_only_unproven_requirements() -> None:
    manifest = _manifest(ProcessMaturity.PC3_OPERATIONAL)

    missing = manifest.missing_for(ProcessMaturity.PC4_DURABLE)

    assert missing == PROCESS_MATURITY_REQUIREMENTS[ProcessMaturity.PC4_DURABLE]


def test_pc5_is_complete_process_canonical_and_pc6_is_integrated() -> None:
    standalone = _manifest(ProcessMaturity.PC5_OBSERVABLE)
    integrated = _manifest(ProcessMaturity.PC6_COMPOSABLE)

    assert standalone.is_complete_process_canonical
    assert not standalone.is_integrated_process_canonical
    assert integrated.is_complete_process_canonical
    assert integrated.is_integrated_process_canonical


def test_declared_details_are_required_when_corresponding_evidence_is_claimed() -> None:
    evidence = _evidence_through(ProcessMaturity.PC5_OBSERVABLE)

    try:
        ProcessManifest(domain="example", evidence=evidence)
    except ValueError as exc:
        message = str(exc)
    else:  # pragma: no cover - contract guard
        raise AssertionError("manifest accepted evidence without supporting details")

    assert "trigger" in message
    assert "terminal_outcomes" in message
    assert "resources" in message
    assert "sad_paths" in message
    assert "kpis" in message
    assert "specification_path" in message


def test_pc6_requires_named_ingress_and_egress_contracts() -> None:
    evidence = _evidence_through(ProcessMaturity.PC6_COMPOSABLE)

    try:
        ProcessManifest(
            domain="example",
            evidence=evidence,
            trigger="request_created",
            terminal_outcomes=frozenset({"completed"}),
            resources=frozenset({"worker"}),
            sad_paths=frozenset({"rejected"}),
            kpis=frozenset({"lead_time"}),
            specification_path="specification.md",
        )
    except ValueError as exc:
        message = str(exc)
    else:  # pragma: no cover - contract guard
        raise AssertionError("PC6 evidence accepted without integration contracts")

    assert "ingress_contracts" in message
    assert "egress_contracts" in message


def test_complete_assessment_requires_provenance_for_every_nonbaseline_claim() -> None:
    evidence = _evidence_through(ProcessMaturity.PC1_BEHAVIORAL)
    sources = _sources_for(evidence)
    sources.pop(ProcessEvidence.HAPPY_PATH)

    try:
        ProcessManifest(
            domain="example",
            evidence=evidence,
            trigger="request_created",
            evidence_sources=sources,
            assessment_complete=True,
        )
    except ValueError as exc:
        message = str(exc)
    else:  # pragma: no cover - contract guard
        raise AssertionError("audited evidence was accepted without provenance")

    assert "happy_path" in message
    assert "provenance" in message


def test_complete_assessment_accepts_multiple_sources_for_one_claim() -> None:
    evidence = _evidence_through(ProcessMaturity.PC1_BEHAVIORAL)
    sources = _sources_for(evidence)
    sources[ProcessEvidence.HAPPY_PATH] = (
        "tests/integration/test_happy_path.py",
        "docs/examples/example/specification.md",
    )

    manifest = ProcessManifest(
        domain="example",
        evidence=evidence,
        trigger="request_created",
        evidence_sources=sources,
        assessment_complete=True,
    )

    assert manifest.evidence_sources[ProcessEvidence.HAPPY_PATH] == (
        "tests/integration/test_happy_path.py",
        "docs/examples/example/specification.md",
    )


def test_provenance_cannot_claim_undeclared_evidence() -> None:
    evidence = _evidence_through(ProcessMaturity.PC0_REGISTERED)

    try:
        ProcessManifest(
            domain="example",
            evidence=evidence,
            evidence_sources={
                ProcessEvidence.HAPPY_PATH: ("tests/test_happy_path.py",),
            },
        )
    except ValueError as exc:
        message = str(exc)
    else:  # pragma: no cover - contract guard
        raise AssertionError("manifest accepted provenance for undeclared evidence")

    assert "undeclared" in message
    assert "happy_path" in message


def test_provenance_paths_must_be_nonempty_repository_relative_paths() -> None:
    evidence = _evidence_through(ProcessMaturity.PC1_BEHAVIORAL)

    for invalid in ("", "/tmp/evidence.py", "../outside.py"):
        sources = _sources_for(evidence)
        sources[ProcessEvidence.HAPPY_PATH] = (invalid,)
        try:
            ProcessManifest(
                domain="example",
                evidence=evidence,
                trigger="request_created",
                evidence_sources=sources,
            )
        except ValueError as exc:
            message = str(exc)
        else:  # pragma: no cover - contract guard
            raise AssertionError(f"manifest accepted invalid provenance path: {invalid!r}")
        assert "repository-relative" in message

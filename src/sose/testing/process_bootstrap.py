from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping

from sose.examples.process_manifest import EvidenceSources, ProcessEvidence
from sose.testing.conformance import ReferenceCapability, ReferenceContract


# The reference-conformance vocabulary predates process-canonical maturity.
# Only direct semantic equivalents are translated automatically.
REFERENCE_CAPABILITY_PROCESS_EVIDENCE: Mapping[
    ReferenceCapability, ProcessEvidence
] = {
    ReferenceCapability.STATECHARTS: ProcessEvidence.STATECHARTS,
    ReferenceCapability.HAPPY_PATH: ProcessEvidence.HAPPY_PATH,
    ReferenceCapability.SAD_PATHS: ProcessEvidence.SAD_PATHS,
    ReferenceCapability.RESTART_EQUIVALENCE: ProcessEvidence.RESTART_EQUIVALENCE,
    ReferenceCapability.RESOURCES: ProcessEvidence.FINITE_RESOURCES,
    ReferenceCapability.SCHEDULED_WORK: ProcessEvidence.TIME_SEMANTICS,
    ReferenceCapability.CRASH_RECOVERY: ProcessEvidence.FAULT_RECOVERY,
}


@dataclass(frozen=True, slots=True)
class ReferenceProcessEvidenceBootstrap:
    """Candidate process evidence imported from reference conformance.

    This is deliberately not a ProcessManifest: the older reference contract can
    prove behaviors while lacking process metadata such as trigger names,
    resource identities, sad-path names, terminal outcomes, or KPI semantics.
    """

    domain: str
    evidence: frozenset[ProcessEvidence]
    evidence_sources: EvidenceSources
    unmapped_capabilities: frozenset[ReferenceCapability]


def _is_repository_relative(path: str) -> bool:
    from pathlib import PurePosixPath

    if not path or "\\" in path:
        return False
    parsed = PurePosixPath(path)
    return not parsed.is_absolute() and ".." not in parsed.parts and str(parsed) == path


def reference_contract_process_evidence(
    contract: ReferenceContract,
) -> ReferenceProcessEvidenceBootstrap:
    """Translate only directly equivalent reference capabilities into candidates."""

    prefix = "sose.examples."
    if not contract.package.startswith(prefix):
        raise ValueError(
            "reference process bootstrap requires a package under sose.examples"
        )
    domain = contract.package.removeprefix(prefix)
    if not domain or "." in domain:
        raise ValueError(
            "reference process bootstrap requires a direct sose.examples.<domain> package"
        )

    evidence: set[ProcessEvidence] = set()
    sources: dict[ProcessEvidence, tuple[str, ...]] = {}
    mapped_capabilities: set[ReferenceCapability] = set()
    for capability in sorted(contract.capabilities, key=lambda item: item.value):
        process_evidence = REFERENCE_CAPABILITY_PROCESS_EVIDENCE.get(capability)
        if process_evidence is None:
            continue
        paths = tuple(contract.evidence.get(capability, ()))
        if not paths or any(not _is_repository_relative(path) for path in paths):
            raise ValueError(
                f"reference capability {capability.value} requires repository-relative provenance"
            )
        evidence.add(process_evidence)
        sources[process_evidence] = paths
        mapped_capabilities.add(capability)

    return ReferenceProcessEvidenceBootstrap(
        domain=domain,
        evidence=frozenset(evidence),
        evidence_sources=EvidenceSources(
            tuple(sorted(sources.items(), key=lambda pair: pair[0].value))
        ),
        unmapped_capabilities=frozenset(contract.capabilities - mapped_capabilities),
    )

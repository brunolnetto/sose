from __future__ import annotations

from sose.examples.process_manifest import (
    PROCESS_MATURITY_REQUIREMENTS,
    ProcessEvidence,
    ProcessManifest,
    ProcessMaturity,
    bootstrap_reference_contract,
    builtin_process_manifests,
)
from sose.testing.conformance import ReferenceCapability, ReferenceContract
from tests.support.reference_catalog import REFERENCE_CATALOG


DIRECT_CAPABILITY_MAPPING = {
    ReferenceCapability.STATECHARTS: ProcessEvidence.STATECHARTS,
    ReferenceCapability.HAPPY_PATH: ProcessEvidence.HAPPY_PATH,
    ReferenceCapability.SAD_PATHS: ProcessEvidence.SAD_PATHS,
    ReferenceCapability.RESTART_EQUIVALENCE: ProcessEvidence.RESTART_EQUIVALENCE,
    ReferenceCapability.RESOURCES: ProcessEvidence.FINITE_RESOURCES,
    ReferenceCapability.SCHEDULED_WORK: ProcessEvidence.TIME_SEMANTICS,
    ReferenceCapability.CRASH_RECOVERY: ProcessEvidence.FAULT_RECOVERY,
}


def _contract_with_every_capability() -> ReferenceContract:
    capabilities = frozenset(ReferenceCapability)
    return ReferenceContract(
        domain="Example",
        package="sose.examples.example",
        docs_dir="docs/examples/example",
        capabilities=capabilities,
        evidence={
            capability: (f"tests/evidence/{capability.value}.py",)
            for capability in capabilities
        },
    )


def _registered_manifest(domain: str = "example") -> ProcessManifest:
    return ProcessManifest(
        domain=domain,
        evidence=PROCESS_MATURITY_REQUIREMENTS[ProcessMaturity.PC0_REGISTERED],
    )


def test_bootstrap_maps_only_directly_equivalent_reference_capabilities() -> None:
    bootstrapped = bootstrap_reference_contract(
        _registered_manifest(),
        _contract_with_every_capability(),
    )

    expected = (
        PROCESS_MATURITY_REQUIREMENTS[ProcessMaturity.PC0_REGISTERED]
        | frozenset(DIRECT_CAPABILITY_MAPPING.values())
    )
    assert bootstrapped.evidence == expected

    # These require direct process-canonical review; the older reference catalog
    # must not be stretched into stronger claims merely because nearby evidence exists.
    for unproven in {
        ProcessEvidence.ENTITIES,
        ProcessEvidence.COMMAND_EVENT_PATH,
        ProcessEvidence.E2E_TERMINAL_OUTCOME,
        ProcessEvidence.CAPACITY_CONTENTION,
        ProcessEvidence.DURABLE_STATE,
        ProcessEvidence.REPLAY_IDEMPOTENCE,
        ProcessEvidence.RECURRING_RECONCILIATION,
        ProcessEvidence.KPIS,
        ProcessEvidence.ERD,
        ProcessEvidence.PROCESS_DIAGRAM,
        ProcessEvidence.PROJECTION_CONTRACT,
    }:
        assert unproven not in bootstrapped.evidence

    assert not bootstrapped.assessment_complete
    assert bootstrapped.is_maturity_lower_bound


def test_bootstrap_preserves_exact_provenance_for_mapped_capabilities() -> None:
    contract = _contract_with_every_capability()
    bootstrapped = bootstrap_reference_contract(_registered_manifest(), contract)

    for capability, process_evidence in DIRECT_CAPABILITY_MAPPING.items():
        assert bootstrapped.evidence_sources[process_evidence] == contract.evidence[capability]

    assert set(bootstrapped.evidence_sources) == set(DIRECT_CAPABILITY_MAPPING.values())


def test_bootstrap_preserves_existing_process_evidence_and_provenance() -> None:
    manifest = ProcessManifest(
        domain="example",
        evidence=(
            PROCESS_MATURITY_REQUIREMENTS[ProcessMaturity.PC0_REGISTERED]
            | {ProcessEvidence.ENTITIES}
        ),
        evidence_sources={
            ProcessEvidence.ENTITIES: ("src/sose/examples/example/entities.py",),
        },
    )

    bootstrapped = bootstrap_reference_contract(manifest, _contract_with_every_capability())

    assert ProcessEvidence.ENTITIES in bootstrapped.evidence
    assert bootstrapped.evidence_sources[ProcessEvidence.ENTITIES] == (
        "src/sose/examples/example/entities.py",
    )


def test_bootstrap_rejects_reference_contract_for_a_different_domain_package() -> None:
    contract = ReferenceContract(
        domain="Other",
        package="sose.examples.other",
        docs_dir="docs/examples/other",
        capabilities=frozenset({ReferenceCapability.HAPPY_PATH}),
        evidence={
            ReferenceCapability.HAPPY_PATH: ("tests/other/test_happy.py",),
        },
    )

    try:
        bootstrap_reference_contract(_registered_manifest("example"), contract)
    except ValueError as exc:
        message = str(exc)
    else:  # pragma: no cover - contract guard
        raise AssertionError("bootstrap accepted evidence from a different domain")

    assert "other" in message
    assert "example" in message


def test_existing_reference_catalog_can_bootstrap_all_matching_business_domains() -> None:
    manifests = builtin_process_manifests()
    bootstrapped_domains: set[str] = set()

    for contract in REFERENCE_CATALOG:
        domain = contract.package.removeprefix("sose.examples.")
        assert domain in manifests
        bootstrapped = bootstrap_reference_contract(manifests[domain], contract)
        bootstrapped_domains.add(domain)
        assert bootstrapped.evidence_sources
        assert not bootstrapped.assessment_complete

    assert len(bootstrapped_domains) == len(REFERENCE_CATALOG) == 17

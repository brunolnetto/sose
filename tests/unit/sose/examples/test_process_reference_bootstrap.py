from __future__ import annotations

from sose.examples.process_manifest import ProcessEvidence, builtin_process_manifests
from sose.testing.conformance import ReferenceCapability, ReferenceContract
from sose.testing.process_bootstrap import reference_contract_process_evidence
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


def test_bootstrap_maps_only_directly_equivalent_reference_capabilities() -> None:
    bootstrap = reference_contract_process_evidence(_contract_with_every_capability())

    assert bootstrap.domain == "example"
    assert bootstrap.evidence == frozenset(DIRECT_CAPABILITY_MAPPING.values())

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
        assert unproven not in bootstrap.evidence


def test_bootstrap_preserves_exact_provenance_for_mapped_capabilities() -> None:
    contract = _contract_with_every_capability()
    bootstrap = reference_contract_process_evidence(contract)

    for capability, process_evidence in DIRECT_CAPABILITY_MAPPING.items():
        assert bootstrap.evidence_sources[process_evidence] == contract.evidence[capability]

    assert set(bootstrap.evidence_sources) == set(DIRECT_CAPABILITY_MAPPING.values())


def test_unmapped_reference_capabilities_remain_visible_as_unconsumed_evidence() -> None:
    contract = _contract_with_every_capability()
    bootstrap = reference_contract_process_evidence(contract)

    expected_unmapped = frozenset(ReferenceCapability) - set(DIRECT_CAPABILITY_MAPPING)
    assert bootstrap.unmapped_capabilities == expected_unmapped


def test_bootstrap_rejects_packages_outside_builtin_example_namespace() -> None:
    contract = ReferenceContract(
        domain="Other",
        package="external.examples.other",
        docs_dir="docs/examples/other",
        capabilities=frozenset({ReferenceCapability.HAPPY_PATH}),
        evidence={
            ReferenceCapability.HAPPY_PATH: ("tests/other/test_happy.py",),
        },
    )

    try:
        reference_contract_process_evidence(contract)
    except ValueError as exc:
        message = str(exc)
    else:  # pragma: no cover - contract guard
        raise AssertionError("bootstrap accepted a non-SOSE example package")

    assert "sose.examples" in message


def test_existing_reference_catalog_bootstraps_current_business_domain_coverage() -> None:
    manifests = builtin_process_manifests()
    bootstrapped_domains = {
        reference_contract_process_evidence(contract).domain
        for contract in REFERENCE_CATALOG
    }

    assert bootstrapped_domains <= set(manifests)
    # Warehouse Management was added after the reference catalog contract set and
    # is already directly audited under the newer PC evidence model.
    assert set(manifests) - bootstrapped_domains == {"warehouse_management"}
    assert len(bootstrapped_domains) == len(REFERENCE_CATALOG)

    for contract in REFERENCE_CATALOG:
        bootstrap = reference_contract_process_evidence(contract)
        assert bootstrap.evidence_sources
        # Candidate evidence cannot itself claim process-audit completion.
        assert not hasattr(bootstrap, "assessment_complete")

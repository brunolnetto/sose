from pathlib import Path

from sose.testing.conformance import (
    BASELINE_REFERENCE_CAPABILITIES,
    ReferenceCapability,
    ReferenceContract,
    validate_reference_catalog,
    validate_reference_contract,
)
from tests.support.reference_catalog import REFERENCE_CATALOG


REPO_ROOT = Path(__file__).resolve().parents[1]


def test_reference_catalog_has_all_fourteen_promoted_domains():
    assert len(REFERENCE_CATALOG) == 14
    assert {contract.domain for contract in REFERENCE_CATALOG} == {
        "Maintenance / MRO",
        "Procure-to-Pay",
        "Manufacturing",
        "Logistics & transport",
        "Cards & payments",
        "IT service management",
        "Hospitals",
        "Construction",
        "Order-to-Cash",
        "Record-to-Report",
        "Insurance",
        "Airports",
        "Aviation",
        "Credit and loans",
    }


def test_all_reference_contracts_conform():
    issues = validate_reference_catalog(
        REFERENCE_CATALOG,
        repo_root=REPO_ROOT,
    )
    assert issues == (), "\n".join(
        f"[{issue.domain}] {issue.code}: {issue.message}"
        for issue in issues
    )


def test_contract_requires_baseline_capabilities(tmp_path):
    contract = ReferenceContract(
        domain="Incomplete",
        package="missing.package",
        docs_dir="docs/examples/missing",
        capabilities=frozenset({ReferenceCapability.STATECHARTS}),
        evidence={ReferenceCapability.STATECHARTS: ("tests/test_missing.py",)},
    )
    issues = validate_reference_contract(contract, repo_root=tmp_path)
    missing = next(
        issue
        for issue in issues
        if issue.code == "missing-baseline-capability"
    )
    assert "happy_path" in missing.message
    assert "sad_paths" in missing.message
    assert "restart_equivalence" in missing.message


def test_baseline_is_deliberately_small_and_semantic():
    assert BASELINE_REFERENCE_CAPABILITIES == {
        ReferenceCapability.STATECHARTS,
        ReferenceCapability.HAPPY_PATH,
        ReferenceCapability.SAD_PATHS,
        ReferenceCapability.RESTART_EQUIVALENCE,
    }

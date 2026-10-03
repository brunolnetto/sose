from pathlib import Path

from sose.testing.conformance import (
    BASELINE_REFERENCE_CAPABILITIES,
    CAPABILITY_REQUIREMENTS,
    ReferenceCapability,
    ReferenceContract,
    _test_file_has_test,
    validate_reference_catalog,
    validate_reference_contract,
)
from tests.support.reference_catalog import REFERENCE_CATALOG


REPO_ROOT = Path(__file__).resolve().parents[1]


def test_reference_catalog_has_all_promoted_domains():
    expected_domains = {
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
        "Telecommunications",
        "Energy / Utilities",
        "Public Transit / Rail",
        "Field Service / Workforce",
        "Hospitality / Reservations",
        "Warehouse / Fulfillment",
        "Subscription / SaaS",
    }
    assert len(REFERENCE_CATALOG) == len(expected_domains)
    assert {contract.domain for contract in REFERENCE_CATALOG} == expected_domains


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


def test_capability_dependencies_are_enforced(tmp_path):
    contract = ReferenceContract(
        domain="Bad preemption",
        package="missing.package",
        docs_dir="docs/examples/missing",
        capabilities=frozenset({
            ReferenceCapability.STATECHARTS,
            ReferenceCapability.HAPPY_PATH,
            ReferenceCapability.SAD_PATHS,
            ReferenceCapability.RESTART_EQUIVALENCE,
            ReferenceCapability.PREEMPTION,
        }),
        evidence={
            ReferenceCapability.STATECHARTS: ("tests/test_statecharts.py",),
            ReferenceCapability.HAPPY_PATH: ("tests/test_happy.py",),
            ReferenceCapability.SAD_PATHS: ("tests/test_sad.py",),
            ReferenceCapability.RESTART_EQUIVALENCE: ("tests/test_restart.py",),
            ReferenceCapability.PREEMPTION: ("tests/test_preemption.py",),
        },
    )

    issues = validate_reference_contract(contract, repo_root=tmp_path)

    dependency = next(
        issue
        for issue in issues
        if issue.code == "missing-capability-dependency"
    )
    assert dependency.domain == "Bad preemption"
    assert "preemption requires: resources" in dependency.message


def test_capability_requirement_graph_stays_semantic():
    assert CAPABILITY_REQUIREMENTS[ReferenceCapability.PREEMPTION] == {
        ReferenceCapability.RESOURCES
    }
    assert CAPABILITY_REQUIREMENTS[ReferenceCapability.SCHEDULED_WORK] == {
        ReferenceCapability.RESTART_EQUIVALENCE
    }
    assert CAPABILITY_REQUIREMENTS[ReferenceCapability.STORE_SELECTION] == {
        ReferenceCapability.RESTART_EQUIVALENCE
    }


def _minimal_contract(*, docs_dir: str, evidence_path: str) -> ReferenceContract:
    capabilities = frozenset(BASELINE_REFERENCE_CAPABILITIES)
    return ReferenceContract(
        domain="Fixture",
        package="missing.package",
        docs_dir=docs_dir,
        capabilities=capabilities,
        evidence={
            capability: (evidence_path,)
            for capability in capabilities
        },
    )


def test_evidence_requires_collectable_top_level_test(tmp_path):
    evidence = tmp_path / "tests" / "test_fake.py"
    evidence.parent.mkdir(parents=True)
    evidence.write_text(
        '"""def test_in_a_string(): pass"""\n'
        "# def test_in_a_comment(): pass\n"
        "def helper():\n"
        "    def test_nested():\n"
        "        pass\n",
        encoding="utf-8",
    )
    docs = tmp_path / "docs" / "fixture"
    docs.mkdir(parents=True)
    (docs / "README.md").write_text(
        "Status: **Reference implementation**\n",
        encoding="utf-8",
    )
    (docs / "specification.md").write_text(
        "Current status: **Reference implementation**.\n",
        encoding="utf-8",
    )

    issues = validate_reference_contract(
        _minimal_contract(
            docs_dir="docs/fixture",
            evidence_path="tests/test_fake.py",
        ),
        repo_root=tmp_path,
    )

    assert any(issue.code == "empty-test-evidence" for issue in issues)


def test_documentation_status_rejects_candidate_wording(tmp_path):
    evidence = tmp_path / "tests" / "test_real.py"
    evidence.parent.mkdir(parents=True)
    evidence.write_text("def test_real():\n    pass\n", encoding="utf-8")
    docs = tmp_path / "docs" / "fixture"
    docs.mkdir(parents=True)
    (docs / "README.md").write_text(
        "## Status\n\n**Reference implementation candidate.**\n",
        encoding="utf-8",
    )
    (docs / "specification.md").write_text(
        "Promote to Reference implementation when CI is green.\n",
        encoding="utf-8",
    )
    contract = _minimal_contract(
        docs_dir="docs/fixture",
        evidence_path="tests/test_real.py",
    )

    issues = validate_reference_contract(contract, repo_root=tmp_path)

    assert any(issue.code == "documentation-status-drift" for issue in issues)

    (docs / "README.md").write_text(
        "## Status\n\n**Reference implementation.**\n",
        encoding="utf-8",
    )
    issues = validate_reference_contract(contract, repo_root=tmp_path)
    assert not any(
        issue.code == "documentation-status-drift" for issue in issues
    )


def test_scenario_module_import_failure_is_reported(tmp_path):
    capabilities = frozenset({
        *BASELINE_REFERENCE_CAPABILITIES,
        ReferenceCapability.SCENARIOS,
    })
    contract = ReferenceContract(
        domain="Broken scenarios",
        package="missing.scenario_package",
        docs_dir="docs/missing",
        capabilities=capabilities,
        evidence={
            capability: ("tests/test_missing.py",)
            for capability in capabilities
        },
    )

    issues = validate_reference_contract(contract, repo_root=tmp_path)

    scenario_issue = next(
        issue
        for issue in issues
        if issue.code == "scenario-module-import-failed"
    )
    assert scenario_issue.domain == "Broken scenarios"
    assert "missing.scenario_package.scenarios" in scenario_issue.message


def test_test_file_has_test_rejects_invalid_utf8_and_syntax(tmp_path):
    binary = tmp_path / "tests" / "test_binary.py"
    binary.parent.mkdir(parents=True)
    binary.write_bytes(b"\xff\xfe")
    assert _test_file_has_test(binary) is False

    broken = tmp_path / "tests" / "test_broken.py"
    broken.write_text("def test_bad(:\n    pass\n", encoding="utf-8")
    assert _test_file_has_test(broken) is False


def test_contract_reports_undeclared_and_missing_evidence(tmp_path):
    evidence = tmp_path / "tests" / "test_real.py"
    evidence.parent.mkdir(parents=True)
    evidence.write_text("def test_real():\n    pass\n", encoding="utf-8")
    docs = tmp_path / "docs" / "fixture"
    docs.mkdir(parents=True)
    (docs / "README.md").write_text(
        "Status: **Reference implementation**\n",
        encoding="utf-8",
    )
    (docs / "specification.md").write_text(
        "Current status: **Reference implementation**.\n",
        encoding="utf-8",
    )

    capabilities = frozenset(BASELINE_REFERENCE_CAPABILITIES)
    contract = ReferenceContract(
        domain="Evidence mismatch",
        package="missing.package",
        docs_dir="docs/fixture",
        capabilities=capabilities,
        evidence={
            ReferenceCapability.STATECHARTS: ("tests/test_real.py",),
            ReferenceCapability.HAPPY_PATH: (),
            ReferenceCapability.SAD_PATHS: ("tests/test_real.py",),
            ReferenceCapability.RESTART_EQUIVALENCE: ("tests/test_real.py",),
            ReferenceCapability.SCENARIOS: ("tests/test_real.py",),
        },
    )

    issues = validate_reference_contract(contract, repo_root=tmp_path)
    codes = {issue.code for issue in issues}
    assert "evidence-for-undeclared-capability" in codes
    assert "missing-evidence" in codes


def test_contract_reports_missing_runtime_entrypoints(tmp_path, monkeypatch):
    package = tmp_path / "demo_pkg"
    package.mkdir()
    (package / "__init__.py").write_text("", encoding="utf-8")
    (package / "entities.py").write_text("", encoding="utf-8")
    (package / "statecharts.py").write_text("", encoding="utf-8")
    (package / "simulation.py").write_text(
        "def build_runtime(*args, **kwargs):\n    return None\n",
        encoding="utf-8",
    )
    monkeypatch.syspath_prepend(str(tmp_path))

    tests_dir = tmp_path / "tests"
    tests_dir.mkdir()
    (tests_dir / "test_real.py").write_text(
        "def test_real():\n    pass\n",
        encoding="utf-8",
    )
    docs = tmp_path / "docs" / "fixture"
    docs.mkdir(parents=True)
    (docs / "README.md").write_text(
        "Status: **Reference implementation**\n",
        encoding="utf-8",
    )
    (docs / "specification.md").write_text(
        "Current status: **Reference implementation**.\n",
        encoding="utf-8",
    )

    contract = ReferenceContract(
        domain="Missing runtime entrypoint",
        package="demo_pkg",
        docs_dir="docs/fixture",
        capabilities=frozenset(BASELINE_REFERENCE_CAPABILITIES),
        evidence={
            capability: ("tests/test_real.py",)
            for capability in BASELINE_REFERENCE_CAPABILITIES
        },
    )

    issues = validate_reference_contract(contract, repo_root=tmp_path)
    assert any(issue.code == "runtime-entrypoint-missing" for issue in issues)


def test_catalog_reports_duplicate_identity_fields(tmp_path):
    docs = tmp_path / "docs" / "fixture"
    docs.mkdir(parents=True)
    (docs / "README.md").write_text(
        "Status: **Reference implementation**\n",
        encoding="utf-8",
    )
    (docs / "specification.md").write_text(
        "Current status: **Reference implementation**.\n",
        encoding="utf-8",
    )
    tests_dir = tmp_path / "tests"
    tests_dir.mkdir()
    (tests_dir / "test_real.py").write_text(
        "def test_real():\n    pass\n",
        encoding="utf-8",
    )

    capabilities = frozenset(BASELINE_REFERENCE_CAPABILITIES)
    evidence = {
        capability: ("tests/test_real.py",)
        for capability in capabilities
    }
    first = ReferenceContract(
        domain="Same",
        package="pkg.one",
        docs_dir="docs/fixture",
        capabilities=capabilities,
        evidence=evidence,
    )
    second = ReferenceContract(
        domain="Same",
        package="pkg.two",
        docs_dir="docs/fixture-two",
        capabilities=capabilities,
        evidence=evidence,
    )
    docs_two = tmp_path / "docs" / "fixture-two"
    docs_two.mkdir(parents=True)
    (docs_two / "README.md").write_text(
        "Status: **Reference implementation**\n",
        encoding="utf-8",
    )
    (docs_two / "specification.md").write_text(
        "Current status: **Reference implementation**.\n",
        encoding="utf-8",
    )

    issues = validate_reference_catalog((first, second), repo_root=tmp_path)
    assert any(issue.code == "duplicate-domain" for issue in issues)

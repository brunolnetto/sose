"""Evidence guard for the architecture inventory (#494/#495/#496/#477).

This is NOT runtime organization admission or PC6/scientific promotion.
The JSON describes two different kinds of edges:
  * real catalog/process-manifest/consumer-registry evidence;
  * explicitly proposed canonical decompositions and organization templates.
Never derive business ownership, shared pool capacity or a generic enterprise
runtime from the proposed edges.
"""
from __future__ import annotations

import ast
from copy import deepcopy
import json
from pathlib import Path

import pytest

from sose.examples.catalog import builtin_catalog
from sose.examples.process_manifest import (
    PROCESS_AUDIT_EXCLUDED_DOMAINS,
    builtin_process_manifests,
)

ROOT = Path(__file__).resolve().parents[4]
MATRIX = ROOT / "docs/architecture/canonical-domain-organization-matrix.v1.json"

# The wire name is NOT the public process catalog identity. The aliases
# originate from the owning domain implementation (not from the JSON itself).
# Do not allow inventing a different catalog owner by relabelling the matrix.
EXPECTED_WIRE_ALIASES = {
    "trading_customer": {},
    "trading_replenishment": {"procure_to_pay": "p2p"},
}



def _matrix() -> dict:
    return json.loads(MATRIX.read_text(encoding="utf-8"))


def _unique_ids(items: list[dict], *, field: str = "id") -> set[str]:
    ids = [x[field] for x in items]
    assert len(ids) == len(set(ids)), f"duplicate catalog key: {field}"
    return set(ids)


def _assert_membership(matrix: dict) -> None:
    catalog = builtin_catalog()
    manifest = builtin_process_manifests()
    canonical_ids = _unique_ids(matrix["canonicals"])
    domain_ids = _unique_ids(matrix["domains"])
    # Canonical simulation problems and business domains are different
    # DomainDefinition(kind=...) registrations in ONE authoritative catalog.
    assert canonical_ids == set(catalog.names(kind="canonical"))
    assert domain_ids == (
        set(catalog.names(kind="domain")) - PROCESS_AUDIT_EXCLUDED_DOMAINS
    )
    assert domain_ids == set(manifest)
    assert canonical_ids.isdisjoint(domain_ids)
    assert len(canonical_ids) == 5
    assert len(domain_ids) == 22
    assert matrix["schema_version"] == 1
    assert matrix["governance"]["not_an_admission"] is True

    for canonical in matrix["canonicals"]:
        assert (ROOT / canonical["definition_path"]).is_file()
        assert canonical["definition_path"].endswith(
            "/canonical/" + canonical["id"] + ".py"
        )

    for domain in matrix["domains"]:
        domain_id = domain["id"]
        actual = manifest[domain_id]
        record = domain["audit"]
        assert (ROOT / domain["definition_path"]).is_file()
        assert record["maturity"] == actual.maturity.name.split("_")[0]
        assert record["assessment_complete"] is actual.assessment_complete
        assert domain["canonical_binding_status"] == (
            "hypothesis_not_executable_registry"
        )
        hypotheses = domain["canonical_pattern_hypotheses"]
        assert hypotheses and len(hypotheses) == len(set(hypotheses))
        assert set(hypotheses) <= canonical_ids

        if not actual.assessment_complete:
            # Unknown is NOT equivalent to "there is no physical resource"
            # or "there is no causal boundary"; preserve null semantics.
            assert domain["process_audit_path"] is None
            assert record["resources"] is None
            assert record["ingress_contracts"] is None
            assert record["egress_contracts"] is None
            assert actual.is_maturity_lower_bound
            continue

        assert (ROOT / domain["process_audit_path"]).is_file()
        assert record["resources"] == sorted(actual.resources)
        assert record["ingress_contracts"] == sorted(actual.ingress_contracts)
        assert record["egress_contracts"] == sorted(actual.egress_contracts)
        # A level advertised in the matrix must be backed by the cumulative
        # reviewed ProcessEvidence and recorded source paths.
        assert actual.is_maturity_lower_bound is False
        for contract in actual.ingress_contracts:
            assert actual.contract_sources("ingress", contract)
        for contract in actual.egress_contracts:
            assert actual.contract_sources("egress", contract)


def _registry_edges(path: str, aliases: dict[str, str]) -> set[tuple]:
    """Read literal real consumer registrations, never an invented edge."""
    module = ast.parse((ROOT / path).read_text(encoding="utf-8"))
    found: set[tuple] = set()
    for node in ast.walk(module):
        if not isinstance(node, ast.Call):
            continue
        if not isinstance(node.func, ast.Attribute) or node.func.attr != "register":
            continue
        kwargs = {kw.arg: kw.value for kw in node.keywords if kw.arg}
        required = ("source_domain", "destination_domain",
                    "contract_name", "contract_version")
        if not all(key in kwargs for key in required):
            continue
        try:
            src, dst, name, version = [
                ast.literal_eval(kwargs[key]) for key in required
            ]
        except (ValueError, TypeError):
            continue
        found.add((
            aliases.get(src, src), aliases.get(dst, dst), name, version,
        ))
    return found


def _assert_flow_test_coverage(flow: dict) -> None:
    """Require a real test that calls the flow AND asserts each contract key.

    A string in a comment/doc or a registered-but-untested consumer does not
    prove that a version actually traverses the executable boundary.
    """
    implementation = ROOT / flow["implementation_path"]
    source = ast.parse(implementation.read_text(encoding="utf-8"))
    assert any(
        isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name == flow["entrypoint"]
        for node in ast.walk(source)
    ), "flow entrypoint must exist in real source"
    tested_contracts: set[str] = set()
    for test_path in flow["test_paths"]:
        tree = ast.parse((ROOT / test_path).read_text(encoding="utf-8"))
        for test in ast.walk(tree):
            if not isinstance(test, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            if not test.name.startswith("test_"):
                continue
            executes_flow = any(
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == flow["entrypoint"]
                for node in ast.walk(test)
            )
            if not executes_flow:
                continue
            for assertion in ast.walk(test):
                if not isinstance(assertion, ast.Assert):
                    continue
                tested_contracts.update(
                    node.value
                    for node in ast.walk(assertion.test)
                    if isinstance(node, ast.Constant)
                    and isinstance(node.value, str)
                )
    for edge in flow["boundary_registrations"]:
        identity = f'{edge["contract_name"]}.v{edge["contract_version"]}'
        assert identity in tested_contracts, (
            "claimed executable contract/version lacks a flow-calling "
            f"test assertion: {flow['id']} / {identity}"
        )


def _assert_organizations(matrix: dict) -> None:
    domains = _unique_ids(matrix["domains"])
    organizations = matrix["organizations"]
    names = _unique_ids(organizations)
    flows = matrix["composed_flows"]
    flow_ids = _unique_ids(flows)
    assert len(organizations) == 10
    assert len(flows) == 2

    membership = set()
    for org in organizations:
        components = org["process_components"]
        component_ids = _unique_ids(components, field="component_id")
        assert component_ids
        assert org["physical_pool_bindings"] == "unbound_not_certified"
        for component in components:
            assert component["domain"] in domains
            assert component["owner_binding"] == "unbound_template"
            membership.add(component["domain"])
        assert len(component_ids) == len(components)
        flow_ref = org["flow_ids"]
        assert len(flow_ref) == len(set(flow_ref))
        assert set(flow_ref) <= flow_ids
        assert org["composition_status"] in (
            "proposed_archetype", "bounded_executable_flows",
        )
        if org["composition_status"] == "proposed_archetype":
            assert not flow_ref
        else:
            assert org["id"] == "trading_company"
            assert len(flow_ref) == 2
    assert membership == domains

    for flow in flows:
        assert flow["organization_id"] in names
        org = next(x for x in organizations
                   if x["id"] == flow["organization_id"])
        assert flow["id"] in org["flow_ids"]
        assert org["composition_status"] == "bounded_executable_flows"
        assert flow["execution_status"] == (
            "bounded_executable_path_not_generic_organization_runtime"
        )
        assert (ROOT / flow["implementation_path"]).is_file()
        assert flow["test_paths"]
        assert all((ROOT / path).is_file() for path in flow["test_paths"])
        assert set(flow["boundary_domain_aliases"].values()) <= domains
        assert flow["boundary_domain_aliases"] == EXPECTED_WIRE_ALIASES[flow["id"]]
        # Tie every exceptional wire namespace back to the concrete
        # implementation's imported business-domain code. A JSON-only alias
        # is not independent provenance for catalog ownership.
        implementation_ast = ast.parse(
            (ROOT / flow["implementation_path"]).read_text(encoding="utf-8")
        )
        imports = {
            node.module
            for node in ast.walk(implementation_ast)
            if isinstance(node, ast.ImportFrom)
        }
        for wire_domain, catalog_domain in flow["boundary_domain_aliases"].items():
            assert f"sose.examples.{catalog_domain}" in imports, (
                "wire alias has no corresponding domain implementation import: "
                f"{wire_domain} -> {catalog_domain}"
            )
        _assert_flow_test_coverage(flow)
        registrations = _registry_edges(
            flow["implementation_path"], flow["boundary_domain_aliases"],
        )
        assert registrations
        component_domains = {c["domain"] for c in org["process_components"]}
        recorded = set()
        for edge in flow["boundary_registrations"]:
            key = (
                edge["producer"], edge["consumer"],
                edge["contract_name"], edge["contract_version"],
            )
            assert key not in recorded, f"duplicate recorded boundary: {key}"
            recorded.add(key)
            assert key in registrations, (
                "architecture matrix claims a boundary not registered in "
                f"the executable consumer registry: {key}"
            )
            assert edge["producer"] != edge["consumer"]
            assert edge["producer"] in component_domains
            assert edge["consumer"] in component_domains
            assert type(edge["contract_version"]) is int
            assert edge["contract_version"] >= 1
        assert recorded == registrations, (
            "the executable registry changed; update the architecture matrix"
        )


def test_architecture_matrix_matches_real_catalog_and_audited_manifest() -> None:
    _assert_membership(_matrix())


def test_organization_templates_and_bounded_causal_paths_are_explicit() -> None:
    _assert_organizations(_matrix())


def test_non_audited_domain_is_not_silently_promoted_to_pc5() -> None:
    matrix = deepcopy(_matrix())
    unreviewed = next(x for x in matrix["domains"]
                      if not x["audit"]["assessment_complete"])
    unreviewed["audit"]["maturity"] = "PC5"
    with pytest.raises(AssertionError):
        _assert_membership(matrix)


def test_unknown_physical_resources_are_not_fabricated_as_empty() -> None:
    matrix = deepcopy(_matrix())
    unreviewed = next(x for x in matrix["domains"]
                      if not x["audit"]["assessment_complete"])
    unreviewed["audit"]["resources"] = []
    with pytest.raises(AssertionError):
        _assert_membership(matrix)


def test_unregistered_causal_edge_cannot_be_admitted_by_documentation() -> None:
    matrix = deepcopy(_matrix())
    flow = matrix["composed_flows"][0]
    flow["boundary_registrations"][0]["contract_name"] = "invented.contract"
    with pytest.raises(AssertionError, match="not registered"):
        _assert_organizations(matrix)


def test_proposed_organization_cannot_claim_executable_flow() -> None:
    matrix = deepcopy(_matrix())
    template = next(o for o in matrix["organizations"]
                    if o["composition_status"] == "proposed_archetype")
    template["flow_ids"] = ["trading_customer"]
    with pytest.raises(AssertionError):
        _assert_organizations(matrix)


def test_wire_alias_cannot_forge_a_different_business_domain_owner() -> None:
    matrix = deepcopy(_matrix())
    flow = next(x for x in matrix["composed_flows"]
                if x["id"] == "trading_replenishment")
    flow["boundary_domain_aliases"] = {"procure_to_pay": "logistics"}
    for edge in flow["boundary_registrations"]:
        if edge["producer"] == "p2p":
            edge["producer"] = "logistics"
        if edge["consumer"] == "p2p":
            edge["consumer"] = "logistics"
    with pytest.raises(AssertionError):
        _assert_organizations(matrix)


def test_registered_but_untested_v2_boundary_is_not_execution_evidence() -> None:
    matrix = deepcopy(_matrix())
    flow = next(x for x in matrix["composed_flows"]
                if x["id"] == "trading_customer")
    flow["test_paths"] = [
        path for path in flow["test_paths"]
        if not path.endswith("test_pc6_two_real_customer_paths.py")
    ]
    with pytest.raises(AssertionError, match="lacks a flow-calling test assertion"):
        _assert_organizations(matrix)

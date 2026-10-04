from __future__ import annotations

import ast
from pathlib import Path

from tests.support.paths import repo_root_from
from tests.support.reference_catalog import REFERENCE_CATALOG


REPO_ROOT = repo_root_from(__file__)

FORBIDDEN_HELPER_NAMES = {
    "_request_resource",
    "_resource_request_exists",
    "_normal_request_exists",
    "_request_exists",
    "_reservation_for",
    "_reservation",
    "_resource_reservation",
    "_preemptive_request_exists",
    "_preemptive_reservation",
    "_release_resource",
    "_release_preemptive",
    "resource_request_exists",
    "resource_reservation",
    "preemptive_request_exists",
    "preemptive_reservation",
}

FORBIDDEN_MANAGER_METHODS = {
    ("resources", "request"),
    ("resources", "cancel_pending"),
    ("resources", "release"),
    ("preemptive_resources", "request"),
    ("preemptive_resources", "cancel_pending"),
    ("preemptive_resources", "release"),
}

FORBIDDEN_PERSISTENCE_METHODS = {
    "delete_scheduled_work",
}


def _attribute_chain(node: ast.AST) -> tuple[str, ...]:
    parts: list[str] = []
    current = node
    while isinstance(current, ast.Attribute):
        parts.append(current.attr)
        current = current.value
    if isinstance(current, ast.Name):
        parts.append(current.id)
    return tuple(reversed(parts))


def _reference_python_files():
    seen: set[Path] = set()
    for contract in REFERENCE_CATALOG:
        package_path = REPO_ROOT / "src" / Path(*contract.package.split("."))
        for path in package_path.rglob("*.py"):
            if path not in seen:
                seen.add(path)
                yield path


def test_reference_domains_use_promoted_durable_lifecycle_apis():
    violations: list[str] = []

    for path in _reference_python_files():
        relative = path.relative_to(REPO_ROOT)
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(relative))

        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                if node.name in FORBIDDEN_HELPER_NAMES:
                    violations.append(
                        f"{relative}:{node.lineno}: local lifecycle helper {node.name}"
                    )

            if not isinstance(node, ast.Call):
                continue

            chain = _attribute_chain(node.func)
            if len(chain) >= 2 and tuple(chain[-2:]) in FORBIDDEN_MANAGER_METHODS:
                violations.append(
                    f"{relative}:{node.lineno}: direct manager call {'.'.join(chain)}"
                )

            if chain and chain[-1] in FORBIDDEN_PERSISTENCE_METHODS:
                violations.append(
                    f"{relative}:{node.lineno}: direct durable deletion {chain[-1]}"
                )

    assert violations == [], "\n".join(violations)

from __future__ import annotations

import ast
from dataclasses import dataclass
from enum import Enum
from importlib import import_module
from pathlib import Path
from types import MappingProxyType
from typing import Mapping


class ReferenceCapability(str, Enum):
    STATECHARTS = "statecharts"
    HAPPY_PATH = "happy_path"
    SAD_PATHS = "sad_paths"
    RESTART_EQUIVALENCE = "restart_equivalence"
    SCENARIOS = "scenarios"
    RESOURCES = "resources"
    SCHEDULED_WORK = "scheduled_work"
    STORE_SELECTION = "store_selection"
    PREEMPTION = "preemption"
    CRASH_RECOVERY = "crash_recovery"
    ILLEGAL_PREREQUISITES = "illegal_prerequisites"
    PROBABILISTIC_TRANSITIONS = "probabilistic_transitions"
    IMMUTABLE_OCCURRENCES = "immutable_occurrences"


BASELINE_REFERENCE_CAPABILITIES = frozenset(
    {
        ReferenceCapability.STATECHARTS,
        ReferenceCapability.HAPPY_PATH,
        ReferenceCapability.SAD_PATHS,
        ReferenceCapability.RESTART_EQUIVALENCE,
    }
)

CAPABILITY_REQUIREMENTS = MappingProxyType(
    {
        ReferenceCapability.SCENARIOS: frozenset(
            {ReferenceCapability.SAD_PATHS}
        ),
        ReferenceCapability.SCHEDULED_WORK: frozenset(
            {ReferenceCapability.RESTART_EQUIVALENCE}
        ),
        ReferenceCapability.STORE_SELECTION: frozenset(
            {ReferenceCapability.RESTART_EQUIVALENCE}
        ),
        ReferenceCapability.PREEMPTION: frozenset(
            {ReferenceCapability.RESOURCES}
        ),
        ReferenceCapability.CRASH_RECOVERY: frozenset(
            {ReferenceCapability.RESTART_EQUIVALENCE}
        ),
        ReferenceCapability.ILLEGAL_PREREQUISITES: frozenset(
            {ReferenceCapability.SAD_PATHS}
        ),
        ReferenceCapability.PROBABILISTIC_TRANSITIONS: frozenset(
            {ReferenceCapability.STATECHARTS}
        ),
    }
)


@dataclass(frozen=True, slots=True)
class ReferenceContract:
    domain: str
    package: str
    docs_dir: str
    capabilities: frozenset[ReferenceCapability]
    evidence: Mapping[ReferenceCapability, tuple[str, ...]]
    runtime_module: str | None = None
    runtime_entrypoints: tuple[str, ...] = ("build_runtime", "seed_reference")

    def __post_init__(self) -> None:
        object.__setattr__(self, "capabilities", frozenset(self.capabilities))
        object.__setattr__(
            self,
            "evidence",
            MappingProxyType(
                {
                    capability: tuple(paths)
                    for capability, paths in self.evidence.items()
                }
            ),
        )

    @property
    def resolved_runtime_module(self) -> str:
        return self.runtime_module or f"{self.package}.simulation"


@dataclass(frozen=True, slots=True)
class ReferenceConformanceIssue:
    domain: str
    code: str
    message: str


def _test_file_has_test(path: Path) -> bool:
    try:
        content = path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return False

    try:
        module = ast.parse(content, filename=str(path))
    except SyntaxError:
        return False

    def is_test_function(node: ast.AST) -> bool:
        return (
            isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name.startswith("test_")
        )

    def is_test_class(node: ast.AST) -> bool:
        return isinstance(node, ast.ClassDef) and node.name.startswith("Test")

    def class_has_test_method(node: ast.ClassDef) -> bool:
        return any(is_test_function(member) for member in node.body)

    for node in module.body:
        if is_test_function(node):
            return True
        if is_test_class(node) and class_has_test_method(node):
            return True
    return False


def _declares_reference_status(content: str) -> bool:
    accepted = {
        "reference implementation",
        "status: reference implementation",
        "current status: reference implementation",
    }
    for line in content.splitlines():
        normalized = line.strip().lower().replace("*", "").rstrip(".")
        if normalized in accepted:
            return True
    return False


def _validate_baseline_capabilities(
    contract: ReferenceContract,
    issues: list[ReferenceConformanceIssue],
) -> None:
    missing_baseline = BASELINE_REFERENCE_CAPABILITIES - contract.capabilities
    if not missing_baseline:
        return
    issues.append(
        ReferenceConformanceIssue(
            contract.domain,
            "missing-baseline-capability",
            "missing baseline capabilities: "
            + ", ".join(sorted(cap.value for cap in missing_baseline)),
        )
    )


def _validate_capability_dependencies(
    contract: ReferenceContract,
    issues: list[ReferenceConformanceIssue],
) -> None:
    for capability in sorted(contract.capabilities, key=lambda cap: cap.value):
        missing_dependencies = CAPABILITY_REQUIREMENTS.get(
            capability, frozenset()
        ) - contract.capabilities
        if not missing_dependencies:
            continue
        issues.append(
            ReferenceConformanceIssue(
                contract.domain,
                "missing-capability-dependency",
                f"{capability.value} requires: "
                + ", ".join(sorted(dep.value for dep in missing_dependencies)),
            )
        )


def _validate_capability_evidence(
    contract: ReferenceContract,
    issues: list[ReferenceConformanceIssue],
) -> None:
    undeclared_evidence = set(contract.evidence) - set(contract.capabilities)
    if undeclared_evidence:
        issues.append(
            ReferenceConformanceIssue(
                contract.domain,
                "evidence-for-undeclared-capability",
                "evidence declared for capabilities not present in contract: "
                + ", ".join(sorted(cap.value for cap in undeclared_evidence)),
            )
        )

    missing_evidence = [
        capability
        for capability in sorted(contract.capabilities, key=lambda cap: cap.value)
        if not contract.evidence.get(capability)
    ]
    if not missing_evidence:
        return
    issues.append(
        ReferenceConformanceIssue(
            contract.domain,
            "missing-evidence",
            "capabilities without evidence: "
            + ", ".join(cap.value for cap in missing_evidence),
        )
    )


def _validate_evidence_files(
    contract: ReferenceContract,
    *,
    root: Path,
    issues: list[ReferenceConformanceIssue],
) -> None:
    for capability, paths in contract.evidence.items():
        for relative in paths:
            path = root / relative
            if not path.is_file():
                issues.append(
                    ReferenceConformanceIssue(
                        contract.domain,
                        "missing-evidence-file",
                        f"{capability.value}: missing {relative}",
                    )
                )
                continue
            if relative.startswith("tests/") and not _test_file_has_test(path):
                issues.append(
                    ReferenceConformanceIssue(
                        contract.domain,
                        "empty-test-evidence",
                        f"{capability.value}: {relative} contains no test function",
                    )
                )


def _validate_documentation_status(
    contract: ReferenceContract,
    *,
    root: Path,
    issues: list[ReferenceConformanceIssue],
) -> None:
    docs = root / contract.docs_dir
    readme = docs / "README.md"
    specification = docs / "specification.md"
    for path in (readme, specification):
        if path.is_file():
            continue
        issues.append(
            ReferenceConformanceIssue(
                contract.domain,
                "missing-documentation",
                f"missing {path.relative_to(root)}",
            )
        )

    existing_docs = [
        path.read_text(encoding="utf-8")
        for path in (readme, specification)
        if path.is_file()
    ]
    if existing_docs and any(
        _declares_reference_status(content) for content in existing_docs
    ):
        return
    if existing_docs:
        issues.append(
            ReferenceConformanceIssue(
                contract.domain,
                "documentation-status-drift",
                "README/specification do not declare reference implementation status",
            )
        )


def _validate_required_modules(
    contract: ReferenceContract,
    issues: list[ReferenceConformanceIssue],
) -> None:
    for module_name in (
        contract.package,
        f"{contract.package}.entities",
        f"{contract.package}.statecharts",
        contract.resolved_runtime_module,
    ):
        try:
            module = import_module(module_name)
        except Exception as exc:
            issues.append(
                ReferenceConformanceIssue(
                    contract.domain,
                    "module-import-failed",
                    f"{module_name}: {type(exc).__name__}: {exc}",
                )
            )
            continue
        if module_name != contract.resolved_runtime_module:
            continue
        for required in contract.runtime_entrypoints:
            if callable(getattr(module, required, None)):
                continue
            issues.append(
                ReferenceConformanceIssue(
                    contract.domain,
                    "runtime-entrypoint-missing",
                    f"{module_name} has no callable {required}()",
                )
            )


def _validate_scenario_module(
    contract: ReferenceContract,
    issues: list[ReferenceConformanceIssue],
) -> None:
    if ReferenceCapability.SCENARIOS not in contract.capabilities:
        return
    try:
        import_module(f"{contract.package}.scenarios")
    except Exception as exc:
        issues.append(
            ReferenceConformanceIssue(
                contract.domain,
                "scenario-module-import-failed",
                f"{contract.package}.scenarios: {type(exc).__name__}: {exc}",
            )
        )


def validate_reference_contract(
    contract: ReferenceContract,
    *,
    repo_root: str | Path,
) -> tuple[ReferenceConformanceIssue, ...]:
    root = Path(repo_root)
    issues: list[ReferenceConformanceIssue] = []
    _validate_baseline_capabilities(contract, issues)
    _validate_capability_dependencies(contract, issues)
    _validate_capability_evidence(contract, issues)
    _validate_evidence_files(contract, root=root, issues=issues)
    _validate_documentation_status(contract, root=root, issues=issues)
    _validate_required_modules(contract, issues)
    _validate_scenario_module(contract, issues)
    return tuple(issues)


def validate_reference_catalog(
    contracts: tuple[ReferenceContract, ...],
    *,
    repo_root: str | Path,
) -> tuple[ReferenceConformanceIssue, ...]:
    issues: list[ReferenceConformanceIssue] = []
    for field in ("domain", "package", "docs_dir"):
        seen: dict[str, str] = {}
        for contract in contracts:
            value = getattr(contract, field)
            previous = seen.get(value)
            if previous is not None:
                issues.append(
                    ReferenceConformanceIssue(
                        contract.domain,
                        f"duplicate-{field}",
                        f"{value!r} is also used by {previous}",
                    )
                )
            else:
                seen[value] = contract.domain

    for contract in contracts:
        issues.extend(validate_reference_contract(contract, repo_root=repo_root))
    return tuple(issues)

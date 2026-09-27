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

    for node in module.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if node.name.startswith("test_"):
                return True
        if isinstance(node, ast.ClassDef) and node.name.startswith("Test"):
            if any(
                isinstance(member, (ast.FunctionDef, ast.AsyncFunctionDef))
                and member.name.startswith("test_")
                for member in node.body
            ):
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


def validate_reference_contract(
    contract: ReferenceContract,
    *,
    repo_root: str | Path,
) -> tuple[ReferenceConformanceIssue, ...]:
    root = Path(repo_root)
    issues: list[ReferenceConformanceIssue] = []

    missing_baseline = BASELINE_REFERENCE_CAPABILITIES - contract.capabilities
    if missing_baseline:
        issues.append(
            ReferenceConformanceIssue(
                contract.domain,
                "missing-baseline-capability",
                "missing baseline capabilities: "
                + ", ".join(sorted(cap.value for cap in missing_baseline)),
            )
        )

    for capability in sorted(contract.capabilities, key=lambda cap: cap.value):
        missing_dependencies = CAPABILITY_REQUIREMENTS.get(
            capability, frozenset()
        ) - contract.capabilities
        if missing_dependencies:
            issues.append(
                ReferenceConformanceIssue(
                    contract.domain,
                    "missing-capability-dependency",
                    f"{capability.value} requires: "
                    + ", ".join(
                        sorted(dep.value for dep in missing_dependencies)
                    ),
                )
            )

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
    if missing_evidence:
        issues.append(
            ReferenceConformanceIssue(
                contract.domain,
                "missing-evidence",
                "capabilities without evidence: "
                + ", ".join(cap.value for cap in missing_evidence),
            )
        )

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

    docs = root / contract.docs_dir
    readme = docs / "README.md"
    specification = docs / "specification.md"
    for path in (readme, specification):
        if not path.is_file():
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
    if existing_docs and not any(
        _declares_reference_status(content) for content in existing_docs
    ):
        issues.append(
            ReferenceConformanceIssue(
                contract.domain,
                "documentation-status-drift",
                "README/specification do not declare reference implementation status",
            )
        )

    for module_name in (
        contract.package,
        f"{contract.package}.entities",
        f"{contract.package}.statecharts",
        contract.resolved_runtime_module,
    ):
        try:
            module = import_module(module_name)
        except Exception as exc:  # pragma: no cover - detail returned to caller
            issues.append(
                ReferenceConformanceIssue(
                    contract.domain,
                    "module-import-failed",
                    f"{module_name}: {type(exc).__name__}: {exc}",
                )
            )
            continue
        if module_name == contract.resolved_runtime_module:
            for required in contract.runtime_entrypoints:
                if not callable(getattr(module, required, None)):
                    issues.append(
                        ReferenceConformanceIssue(
                            contract.domain,
                            "runtime-entrypoint-missing",
                            f"{module_name} has no callable {required}()",
                        )
                    )

    if ReferenceCapability.SCENARIOS in contract.capabilities:
        try:
            import_module(f"{contract.package}.scenarios")
        except Exception as exc:  # pragma: no cover - detail returned to caller
            issues.append(
                ReferenceConformanceIssue(
                    contract.domain,
                    "scenario-module-import-failed",
                    f"{contract.package}.scenarios: {type(exc).__name__}: {exc}",
                )
            )

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

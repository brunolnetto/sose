from __future__ import annotations

from dataclasses import dataclass
from enum import IntEnum, StrEnum


class ProcessMaturity(IntEnum):
    """Evidence-backed maturity of an executable business-process reference."""

    PC0_REGISTERED = 0
    PC1_BEHAVIORAL = 1
    PC2_PROCESS = 2
    PC3_OPERATIONAL = 3
    PC4_DURABLE = 4
    PC5_OBSERVABLE = 5
    PC6_COMPOSABLE = 6


class ProcessEvidence(StrEnum):
    REGISTERED = "registered"
    CONFIGURABLE_RUNTIME = "configurable_runtime"

    ENTITIES = "entities"
    STATECHARTS = "statecharts"
    COMMAND_EVENT_PATH = "command_event_path"
    HAPPY_PATH = "happy_path"

    E2E_TERMINAL_OUTCOME = "e2e_terminal_outcome"

    SAD_PATHS = "sad_paths"
    FINITE_RESOURCES = "finite_resources"
    QUEUEING = "queueing"
    TIME_SEMANTICS = "time_semantics"

    DURABLE_STATE = "durable_state"
    RESTART_EQUIVALENCE = "restart_equivalence"
    REPLAY_IDEMPOTENCE = "replay_idempotence"
    RECURRING_RECONCILIATION = "recurring_reconciliation"
    FAULT_RECOVERY = "fault_recovery"

    KPIS = "kpis"
    ERD = "erd"
    STATECHART_DOCUMENTATION = "statechart_documentation"
    PROCESS_DIAGRAM = "process_diagram"
    PROJECTION_CONTRACT = "projection_contract"
    CONFIGURATION_DOCUMENTATION = "configuration_documentation"

    INGRESS_CONTRACTS = "ingress_contracts"
    EGRESS_CONTRACTS = "egress_contracts"
    CROSS_DOMAIN_EXECUTION = "cross_domain_execution"


PROCESS_MATURITY_REQUIREMENTS: dict[ProcessMaturity, frozenset[ProcessEvidence]] = {
    ProcessMaturity.PC0_REGISTERED: frozenset(
        {
            ProcessEvidence.REGISTERED,
            ProcessEvidence.CONFIGURABLE_RUNTIME,
        }
    ),
    ProcessMaturity.PC1_BEHAVIORAL: frozenset(
        {
            ProcessEvidence.ENTITIES,
            ProcessEvidence.STATECHARTS,
            ProcessEvidence.COMMAND_EVENT_PATH,
            ProcessEvidence.HAPPY_PATH,
        }
    ),
    ProcessMaturity.PC2_PROCESS: frozenset(
        {ProcessEvidence.E2E_TERMINAL_OUTCOME}
    ),
    ProcessMaturity.PC3_OPERATIONAL: frozenset(
        {
            ProcessEvidence.SAD_PATHS,
            ProcessEvidence.FINITE_RESOURCES,
            ProcessEvidence.QUEUEING,
            ProcessEvidence.TIME_SEMANTICS,
        }
    ),
    ProcessMaturity.PC4_DURABLE: frozenset(
        {
            ProcessEvidence.DURABLE_STATE,
            ProcessEvidence.RESTART_EQUIVALENCE,
            ProcessEvidence.REPLAY_IDEMPOTENCE,
            ProcessEvidence.RECURRING_RECONCILIATION,
            ProcessEvidence.FAULT_RECOVERY,
        }
    ),
    ProcessMaturity.PC5_OBSERVABLE: frozenset(
        {
            ProcessEvidence.KPIS,
            ProcessEvidence.ERD,
            ProcessEvidence.STATECHART_DOCUMENTATION,
            ProcessEvidence.PROCESS_DIAGRAM,
            ProcessEvidence.PROJECTION_CONTRACT,
            ProcessEvidence.CONFIGURATION_DOCUMENTATION,
        }
    ),
    ProcessMaturity.PC6_COMPOSABLE: frozenset(
        {
            ProcessEvidence.INGRESS_CONTRACTS,
            ProcessEvidence.EGRESS_CONTRACTS,
            ProcessEvidence.CROSS_DOMAIN_EXECUTION,
        }
    ),
}


def _requirements_through(level: ProcessMaturity) -> frozenset[ProcessEvidence]:
    requirements: set[ProcessEvidence] = set()
    for maturity in ProcessMaturity:
        if maturity > level:
            break
        requirements.update(PROCESS_MATURITY_REQUIREMENTS[maturity])
    return frozenset(requirements)


@dataclass(frozen=True, slots=True)
class ProcessManifest:
    """Auditable evidence for one process canonical.

    `assessment_complete=False` means the derived maturity is a conservative
    lower bound. Missing evidence in that case must not be interpreted as proof
    that the implementation lacks the behavior; it only means the behavior has
    not yet been audited into this manifest.
    """

    domain: str
    evidence: frozenset[ProcessEvidence]
    trigger: str | None = None
    terminal_outcomes: frozenset[str] = frozenset()
    resources: frozenset[str] = frozenset()
    sad_paths: frozenset[str] = frozenset()
    kpis: frozenset[str] = frozenset()
    specification_path: str | None = None
    ingress_contracts: frozenset[str] = frozenset()
    egress_contracts: frozenset[str] = frozenset()
    assessment_complete: bool = False

    def __post_init__(self) -> None:
        if not self.domain.strip():
            raise ValueError("domain must be non-empty")

        pc0 = PROCESS_MATURITY_REQUIREMENTS[ProcessMaturity.PC0_REGISTERED]
        if not pc0.issubset(self.evidence):
            raise ValueError("process manifest must satisfy the PC0 registered runtime baseline")

        missing_details: list[str] = []
        if ProcessEvidence.HAPPY_PATH in self.evidence and not self.trigger:
            missing_details.append("trigger")
        if (
            ProcessEvidence.E2E_TERMINAL_OUTCOME in self.evidence
            and not self.terminal_outcomes
        ):
            missing_details.append("terminal_outcomes")
        if ProcessEvidence.FINITE_RESOURCES in self.evidence and not self.resources:
            missing_details.append("resources")
        if ProcessEvidence.SAD_PATHS in self.evidence and not self.sad_paths:
            missing_details.append("sad_paths")
        if ProcessEvidence.KPIS in self.evidence and not self.kpis:
            missing_details.append("kpis")

        documentation_evidence = {
            ProcessEvidence.ERD,
            ProcessEvidence.STATECHART_DOCUMENTATION,
            ProcessEvidence.PROCESS_DIAGRAM,
            ProcessEvidence.PROJECTION_CONTRACT,
            ProcessEvidence.CONFIGURATION_DOCUMENTATION,
        }
        if self.evidence.intersection(documentation_evidence) and not self.specification_path:
            missing_details.append("specification_path")

        if ProcessEvidence.INGRESS_CONTRACTS in self.evidence and not self.ingress_contracts:
            missing_details.append("ingress_contracts")
        if ProcessEvidence.EGRESS_CONTRACTS in self.evidence and not self.egress_contracts:
            missing_details.append("egress_contracts")

        if missing_details:
            raise ValueError(
                "process evidence requires supporting manifest details: "
                + ", ".join(sorted(missing_details))
            )

    @property
    def maturity(self) -> ProcessMaturity:
        achieved = ProcessMaturity.PC0_REGISTERED
        for level in ProcessMaturity:
            if PROCESS_MATURITY_REQUIREMENTS[level].issubset(self.evidence):
                achieved = level
                continue
            break
        return achieved

    def missing_for(self, level: ProcessMaturity) -> frozenset[ProcessEvidence]:
        return PROCESS_MATURITY_REQUIREMENTS[level] - self.evidence

    @property
    def is_maturity_lower_bound(self) -> bool:
        return not self.assessment_complete

    @property
    def is_complete_process_canonical(self) -> bool:
        return self.maturity >= ProcessMaturity.PC5_OBSERVABLE

    @property
    def is_integrated_process_canonical(self) -> bool:
        return self.maturity >= ProcessMaturity.PC6_COMPOSABLE


@dataclass(frozen=True, slots=True)
class ProcessAuditRow:
    domain: str
    maturity: ProcessMaturity
    next_maturity: ProcessMaturity | None
    assessment_is_lower_bound: bool
    missing_for_next_gate: frozenset[ProcessEvidence]


def _registered_only_manifest(domain: str) -> ProcessManifest:
    return ProcessManifest(
        domain=domain,
        evidence=PROCESS_MATURITY_REQUIREMENTS[ProcessMaturity.PC0_REGISTERED],
        assessment_complete=False,
    )


def _warehouse_management_manifest() -> ProcessManifest:
    evidence = _requirements_through(ProcessMaturity.PC5_OBSERVABLE)
    return ProcessManifest(
        domain="warehouse_management",
        evidence=evidence,
        trigger="inter_site_transfer_planned",
        terminal_outcomes=frozenset({"completed"}),
        resources=frozenset({"dock", "forklift", "truck", "stock"}),
        sad_paths=frozenset(
            {
                "insufficient_stock",
                "dock_contention",
                "forklift_contention",
                "corrupt_handling_ownership",
                "inconsistent_reserved_stock",
                "crash_recovery",
            }
        ),
        kpis=frozenset({"lead_time", "lateness", "on_time"}),
        specification_path="src/sose/examples/warehouse_management/specification.md",
        assessment_complete=True,
    )


def builtin_process_manifests() -> dict[str, ProcessManifest]:
    """Return one explicit process-evidence entry per built-in business domain.

    The initial audit is intentionally conservative. Domains not yet reviewed
    against the process-canonical contract are registered as PC0 lower bounds,
    even when their implementation is known to contain richer behavior.
    """

    from .catalog import builtin_catalog

    manifests = {
        name: _registered_only_manifest(name)
        for name in builtin_catalog().names(kind="domain")
    }
    if "warehouse_management" in manifests:
        manifests["warehouse_management"] = _warehouse_management_manifest()
    return manifests


def audit_builtin_processes() -> tuple[ProcessAuditRow, ...]:
    rows: list[ProcessAuditRow] = []
    for manifest in builtin_process_manifests().values():
        if manifest.maturity is ProcessMaturity.PC6_COMPOSABLE:
            next_maturity = None
            missing = frozenset()
        else:
            next_maturity = ProcessMaturity(manifest.maturity + 1)
            missing = manifest.missing_for(next_maturity)
        rows.append(
            ProcessAuditRow(
                domain=manifest.domain,
                maturity=manifest.maturity,
                next_maturity=next_maturity,
                assessment_is_lower_bound=manifest.is_maturity_lower_bound,
                missing_for_next_gate=missing,
            )
        )
    return tuple(sorted(rows, key=lambda row: (-int(row.maturity), row.domain)))

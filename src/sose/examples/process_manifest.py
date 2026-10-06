from __future__ import annotations

from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from enum import IntEnum, StrEnum
from pathlib import PurePosixPath


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
    CAPACITY_CONTENTION = "capacity_contention"
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
            ProcessEvidence.CAPACITY_CONTENTION,
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

# `tutorial_job` deliberately remains a normal `DomainDefinition(kind="domain")`
# because it demonstrates durable recurring-job mechanics through the same public
# APIs as real domains. It is not, however, a business-process canonical candidate
# and therefore does not belong in the PC0-PC6 promotion audit.
PROCESS_AUDIT_EXCLUDED_DOMAINS = frozenset({"tutorial_job"})


def _requirements_through(level: ProcessMaturity) -> frozenset[ProcessEvidence]:
    requirements: set[ProcessEvidence] = set()
    for maturity in ProcessMaturity:
        if maturity > level:
            break
        requirements.update(PROCESS_MATURITY_REQUIREMENTS[maturity])
    return frozenset(requirements)


def _is_repository_relative(path: str) -> bool:
    if not path or "\\" in path:
        return False
    parsed = PurePosixPath(path)
    return not parsed.is_absolute() and ".." not in parsed.parts and str(parsed) == path


@dataclass(frozen=True, slots=True)
class EvidenceSources(Mapping[ProcessEvidence, tuple[str, ...]]):
    """Hashable immutable mapping from a process claim to repository provenance."""

    entries: tuple[tuple[ProcessEvidence, tuple[str, ...]], ...] = ()

    def __getitem__(self, key: ProcessEvidence) -> tuple[str, ...]:
        item = ProcessEvidence(key)
        for evidence, paths in self.entries:
            if evidence is item:
                return paths
        raise KeyError(key)

    def __iter__(self) -> Iterator[ProcessEvidence]:
        return (evidence for evidence, _ in self.entries)

    def __len__(self) -> int:
        return len(self.entries)


@dataclass(frozen=True, slots=True)
class ProcessManifest:
    """Auditable evidence for one process canonical.

    `assessment_complete=False` means the derived maturity is a conservative
    lower bound. Missing evidence in that case must not be interpreted as proof
    that the implementation lacks the behavior; it only means the behavior has
    not yet been audited into this manifest.

    `evidence_sources` binds audited claims to repository-relative provenance.
    Runtime construction does not touch the filesystem; repository tests are
    responsible for asserting that declared source paths still exist.
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
    evidence_sources: Mapping[ProcessEvidence, tuple[str, ...]] = field(
        default_factory=EvidenceSources
    )
    assessment_complete: bool = False

    def __post_init__(self) -> None:
        if not self.domain.strip():
            raise ValueError("domain must be non-empty")

        pc0 = PROCESS_MATURITY_REQUIREMENTS[ProcessMaturity.PC0_REGISTERED]
        if not pc0.issubset(self.evidence):
            raise ValueError("process manifest must satisfy the PC0 registered runtime baseline")

        normalized_sources: dict[ProcessEvidence, tuple[str, ...]] = {}
        for evidence, paths in self.evidence_sources.items():
            item = ProcessEvidence(evidence)
            if isinstance(paths, (str, bytes)) or not isinstance(paths, Sequence):
                raise ValueError(
                    f"provenance for {item.value} must be an explicit path sequence"
                )
            normalized = tuple(paths)
            if item not in self.evidence:
                raise ValueError(
                    f"provenance declared for undeclared evidence: {item.value}"
                )
            if not normalized or any(
                not isinstance(path, str) or not _is_repository_relative(path)
                for path in normalized
            ):
                raise ValueError(
                    f"provenance for {item.value} must use non-empty repository-relative paths"
                )
            normalized_sources[item] = normalized
        object.__setattr__(
            self,
            "evidence_sources",
            EvidenceSources(
                tuple(
                    sorted(
                        normalized_sources.items(),
                        key=lambda pair: pair[0].value,
                    )
                )
            ),
        )

        if self.assessment_complete:
            missing_provenance = (self.evidence - pc0) - set(normalized_sources)
            if missing_provenance:
                raise ValueError(
                    "audited process evidence requires provenance for: "
                    + ", ".join(sorted(item.value for item in missing_provenance))
                )

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


def _warehouse_fulfillment_manifest() -> ProcessManifest:
    """Audit the current Warehouse Fulfillment reference without inferring PC3."""

    evidence = set(_requirements_through(ProcessMaturity.PC2_PROCESS))
    evidence.update(
        {
            ProcessEvidence.SAD_PATHS,
            ProcessEvidence.DURABLE_STATE,
            ProcessEvidence.REPLAY_IDEMPOTENCE,
            ProcessEvidence.RECURRING_RECONCILIATION,
        }
    )

    simulation = "src/sose/examples/warehouse_fulfillment/simulation.py"
    entities = "src/sose/examples/warehouse_fulfillment/entities.py"
    statecharts = "src/sose/examples/warehouse_fulfillment/statecharts.py"
    definition = "src/sose/examples/warehouse_fulfillment/definition.py"
    happy_path = (
        "tests/integration/sose/examples/warehouse_fulfillment/"
        "test_warehouse_fulfillment_happy_path.py"
    )
    statechart_test = (
        "tests/integration/sose/examples/warehouse_fulfillment/"
        "test_warehouse_fulfillment_statecharts.py"
    )
    sad_paths = (
        "tests/integration/sose/examples/warehouse_fulfillment/"
        "test_warehouse_fulfillment_sad_paths.py"
    )
    restart = (
        "tests/e2e/sose/examples/warehouse_fulfillment/"
        "test_warehouse_fulfillment_restart_equivalence.py"
    )
    recurring = "tests/unit/sose/jobs/test_recurring_domain_reconciliation.py"
    specification = "docs/examples/warehouse-fulfillment/specification.md"

    evidence_sources: dict[ProcessEvidence, tuple[str, ...]] = {
        ProcessEvidence.ENTITIES: (entities, statechart_test),
        ProcessEvidence.STATECHARTS: (statecharts, statechart_test),
        ProcessEvidence.COMMAND_EVENT_PATH: (simulation, happy_path),
        ProcessEvidence.HAPPY_PATH: (happy_path,),
        ProcessEvidence.E2E_TERMINAL_OUTCOME: (happy_path,),
        ProcessEvidence.SAD_PATHS: (sad_paths,),
        ProcessEvidence.DURABLE_STATE: (simulation, restart),
        ProcessEvidence.REPLAY_IDEMPOTENCE: (simulation, sad_paths, restart),
        ProcessEvidence.RECURRING_RECONCILIATION: (definition, recurring),
    }
    return ProcessManifest(
        domain="warehouse_fulfillment",
        evidence=frozenset(evidence),
        trigger="fulfillment_order_requested",
        terminal_outcomes=frozenset({"shipped"}),
        sad_paths=frozenset(
            {
                "insufficient_inventory",
                "pack_before_all_allocations_picked",
                "conflicting_correction_replay",
                "correction_below_allocated_quantity",
            }
        ),
        specification_path=specification,
        evidence_sources=evidence_sources,
        assessment_complete=True,
    )


def _warehouse_management_manifest() -> ProcessManifest:
    # Audit evidence is intentionally explicit rather than using
    # _requirements_through(PC5). The domain demonstrates phase-marker recovery
    # and replay-safe effects, but it does not yet contain a continuous-vs-rebuild
    # restart-equivalence test. That missing PC4 requirement must keep maturity at
    # PC3 even though PC5 documentation/KPIs already exist.
    evidence = set(_requirements_through(ProcessMaturity.PC3_OPERATIONAL))
    evidence.update(
        PROCESS_MATURITY_REQUIREMENTS[ProcessMaturity.PC4_DURABLE]
        - {ProcessEvidence.RESTART_EQUIVALENCE}
    )
    evidence.update(PROCESS_MATURITY_REQUIREMENTS[ProcessMaturity.PC5_OBSERVABLE])

    behavior_test = "tests/unit/sose/examples/test_warehouse_management_domain.py"
    definition = "src/sose/examples/warehouse_management/definition.py"
    simulation = "src/sose/examples/warehouse_management/simulation.py"
    entities = "src/sose/examples/warehouse_management/entities.py"
    statecharts = "src/sose/examples/warehouse_management/statecharts.py"
    config = "src/sose/examples/warehouse_management/config.py"
    specification = "src/sose/examples/warehouse_management/specification.md"
    evidence_sources: dict[ProcessEvidence, tuple[str, ...]] = {
        ProcessEvidence.ENTITIES: (entities, behavior_test),
        ProcessEvidence.STATECHARTS: (statecharts, behavior_test),
        ProcessEvidence.COMMAND_EVENT_PATH: (simulation, behavior_test),
        ProcessEvidence.HAPPY_PATH: (behavior_test,),
        ProcessEvidence.E2E_TERMINAL_OUTCOME: (behavior_test,),
        ProcessEvidence.SAD_PATHS: (behavior_test,),
        ProcessEvidence.FINITE_RESOURCES: (simulation, behavior_test),
        ProcessEvidence.CAPACITY_CONTENTION: (behavior_test,),
        ProcessEvidence.TIME_SEMANTICS: (simulation, specification),
        ProcessEvidence.DURABLE_STATE: (simulation, behavior_test),
        ProcessEvidence.REPLAY_IDEMPOTENCE: (simulation, behavior_test),
        ProcessEvidence.RECURRING_RECONCILIATION: (definition, behavior_test),
        ProcessEvidence.FAULT_RECOVERY: (behavior_test,),
        ProcessEvidence.KPIS: (simulation, behavior_test, specification),
        ProcessEvidence.ERD: (specification,),
        ProcessEvidence.STATECHART_DOCUMENTATION: (specification,),
        ProcessEvidence.PROCESS_DIAGRAM: (specification,),
        ProcessEvidence.PROJECTION_CONTRACT: (specification,),
        ProcessEvidence.CONFIGURATION_DOCUMENTATION: (config, specification),
    }
    return ProcessManifest(
        domain="warehouse_management",
        evidence=frozenset(evidence),
        trigger="inter_site_transfer_planned",
        terminal_outcomes=frozenset({"completed"}),
        resources=frozenset({"dock", "forklift", "truck"}),
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
        specification_path=specification,
        evidence_sources=evidence_sources,
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
        if name not in PROCESS_AUDIT_EXCLUDED_DOMAINS
    }
    if "cards_payments" in manifests:
        from .cards_payments.process_audit import process_manifest as cards_process_manifest

        manifests["cards_payments"] = cards_process_manifest()
    if "logistics" in manifests:
        from .logistics.process_audit import process_manifest as logistics_process_manifest

        manifests["logistics"] = logistics_process_manifest()
    if "order_to_cash" in manifests:
        from .order_to_cash.process_audit import process_manifest as o2c_process_manifest

        manifests["order_to_cash"] = o2c_process_manifest()
    if "p2p" in manifests:
        from .p2p.process_audit import process_manifest as p2p_process_manifest

        manifests["p2p"] = p2p_process_manifest()
    if "record_to_report" in manifests:
        from .record_to_report.process_audit import process_manifest as r2r_process_manifest

        manifests["record_to_report"] = r2r_process_manifest()
    if "warehouse_fulfillment" in manifests:
        manifests["warehouse_fulfillment"] = _warehouse_fulfillment_manifest()
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
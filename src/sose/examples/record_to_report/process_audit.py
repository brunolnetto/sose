from __future__ import annotations

from sose.examples.process_manifest import (
    PROCESS_MATURITY_REQUIREMENTS,
    ProcessEvidence,
    ProcessManifest,
    ProcessMaturity,
)


def process_manifest() -> ProcessManifest:
    """Return the reviewed process-canonical evidence for Record-to-Report."""

    evidence: set[ProcessEvidence] = set()
    for level in (
        ProcessMaturity.PC0_REGISTERED,
        ProcessMaturity.PC1_BEHAVIORAL,
        ProcessMaturity.PC2_PROCESS,
        ProcessMaturity.PC3_OPERATIONAL,
        ProcessMaturity.PC4_DURABLE,
    ):
        evidence.update(PROCESS_MATURITY_REQUIREMENTS[level])
    evidence.add(ProcessEvidence.STATECHART_DOCUMENTATION)

    simulation = "src/sose/examples/record_to_report/simulation.py"
    entities = "src/sose/examples/record_to_report/entities.py"
    statecharts = "src/sose/examples/record_to_report/statecharts.py"
    definition = "src/sose/examples/record_to_report/definition.py"
    specification = "docs/examples/record-to-report/specification.md"
    happy_paths = (
        "tests/integration/sose/examples/record_to_report/"
        "test_record_to_report_happy_paths.py"
    )
    invariants = (
        "tests/integration/sose/examples/record_to_report/"
        "test_record_to_report_invariants.py"
    )
    reopen = (
        "tests/integration/sose/examples/record_to_report/"
        "test_record_to_report_reopen.py"
    )
    scenarios = (
        "tests/integration/sose/examples/record_to_report/"
        "test_record_to_report_scenarios.py"
    )
    statechart_test = (
        "tests/integration/sose/examples/record_to_report/"
        "test_record_to_report_statecharts.py"
    )
    edges = (
        "tests/unit/sose/examples/record_to_report/"
        "test_coverage_record_to_report_edges.py"
    )
    restart = (
        "tests/e2e/sose/examples/record_to_report/"
        "test_record_to_report_restart_equivalence.py"
    )
    recurring = "tests/e2e/sose/jobs/test_recurring_reference_complete.py"

    evidence_sources: dict[ProcessEvidence, tuple[str, ...]] = {
        ProcessEvidence.ENTITIES: (entities, happy_paths),
        ProcessEvidence.STATECHARTS: (statecharts, statechart_test, specification),
        ProcessEvidence.COMMAND_EVENT_PATH: (simulation, happy_paths),
        ProcessEvidence.HAPPY_PATH: (happy_paths,),
        ProcessEvidence.E2E_TERMINAL_OUTCOME: (happy_paths,),
        ProcessEvidence.SAD_PATHS: (invariants, reopen, scenarios, edges),
        ProcessEvidence.FINITE_RESOURCES: (simulation, edges, specification),
        ProcessEvidence.CAPACITY_CONTENTION: (edges, restart),
        ProcessEvidence.TIME_SEMANTICS: (simulation, restart, specification),
        ProcessEvidence.DURABLE_STATE: (simulation, restart, reopen),
        ProcessEvidence.RESTART_EQUIVALENCE: (restart,),
        ProcessEvidence.REPLAY_IDEMPOTENCE: (simulation, edges, restart),
        ProcessEvidence.RECURRING_RECONCILIATION: (definition, recurring),
        ProcessEvidence.FAULT_RECOVERY: (restart, scenarios),
        ProcessEvidence.STATECHART_DOCUMENTATION: (statecharts, specification),
    }

    return ProcessManifest(
        domain="record_to_report",
        evidence=frozenset(evidence),
        trigger="journal_drafted",
        terminal_outcomes=frozenset({"close_task_completed"}),
        resources=frozenset(
            {"posting_processor", "reconciliation_analyst", "close_accountant"}
        ),
        sad_paths=frozenset(
            {
                "rejected_posting",
                "unmatched_adjustment",
                "posting_outage",
                "close_accountant_contention",
                "close_team_shortage",
                "controlled_reopen",
            }
        ),
        specification_path=specification,
        evidence_sources=evidence_sources,
        assessment_complete=True,
    )

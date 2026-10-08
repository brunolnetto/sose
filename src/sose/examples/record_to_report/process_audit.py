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
    evidence.update(PROCESS_MATURITY_REQUIREMENTS[ProcessMaturity.PC5_OBSERVABLE])

    simulation = "src/sose/examples/record_to_report/simulation.py"
    entities = "src/sose/examples/record_to_report/entities.py"
    statecharts = "src/sose/examples/record_to_report/statecharts.py"
    definition = "src/sose/examples/record_to_report/definition.py"
    specification = "docs/examples/record-to-report/specification.md"
    observability = "src/sose/examples/record_to_report/observability.py"
    config = "src/sose/examples/record_to_report/config.py"
    observability_test = (
        "tests/unit/sose/examples/record_to_report/"
        "test_record_to_report_observability.py"
    )
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
        ProcessEvidence.KPIS: (observability, observability_test, specification),
        ProcessEvidence.ERD: (specification,),
        ProcessEvidence.STATECHART_DOCUMENTATION: (
            statecharts,
            statechart_test,
            specification,
        ),
        ProcessEvidence.PROCESS_DIAGRAM: (specification,),
        ProcessEvidence.PROJECTION_CONTRACT: (
            observability,
            observability_test,
            specification,
        ),
        ProcessEvidence.CONFIGURATION_DOCUMENTATION: (
            config,
            definition,
            observability_test,
            specification,
        ),
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
        kpis=frozenset(
            {
                "close_cycle_seconds",
                "amount",
                "transition_count",
                "rejected_posting_count",
                "unmatched_count",
                "adjustment_count",
                "adjustment_posted_count",
                "close_count",
                "reopen_count",
                "close_task_completed_count",
                "closed",
            }
        ),
        specification_path=specification,
        evidence_sources=evidence_sources,
        assessment_complete=True,
    )

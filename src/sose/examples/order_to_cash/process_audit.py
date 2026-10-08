from __future__ import annotations

from sose.examples.process_manifest import (
    PROCESS_MATURITY_REQUIREMENTS,
    ProcessEvidence,
    ProcessManifest,
    ProcessMaturity,
)


def process_manifest() -> ProcessManifest:
    """Return the reviewed process-canonical evidence for Order-to-Cash."""

    evidence: set[ProcessEvidence] = set()
    for level in (
        ProcessMaturity.PC0_REGISTERED,
        ProcessMaturity.PC1_BEHAVIORAL,
        ProcessMaturity.PC2_PROCESS,
        ProcessMaturity.PC3_OPERATIONAL,
        ProcessMaturity.PC4_DURABLE,
    ):
        evidence.update(PROCESS_MATURITY_REQUIREMENTS[level])
    evidence.update(
        {
            ProcessEvidence.KPIS,
            ProcessEvidence.ERD,
            ProcessEvidence.STATECHART_DOCUMENTATION,
            ProcessEvidence.PROCESS_DIAGRAM,
            ProcessEvidence.PROJECTION_CONTRACT,
            ProcessEvidence.CONFIGURATION_DOCUMENTATION,
        }
    )

    simulation = "src/sose/examples/order_to_cash/simulation.py"
    entities = "src/sose/examples/order_to_cash/entities.py"
    statecharts = "src/sose/examples/order_to_cash/statecharts.py"
    definition = "src/sose/examples/order_to_cash/definition.py"
    specification = "docs/examples/order-to-cash/specification.md"
    observability = "src/sose/examples/order_to_cash/observability.py"
    config = "src/sose/examples/order_to_cash/config.py"
    observability_test = (
        "tests/unit/sose/examples/order_to_cash/"
        "test_order_to_cash_observability.py"
    )
    runtime = (
        "tests/unit/sose/examples/order_to_cash/"
        "test_order_to_cash_runtime.py"
    )
    scenarios = (
        "tests/integration/sose/examples/order_to_cash/"
        "test_order_to_cash_scenarios.py"
    )
    statechart_test = (
        "tests/integration/sose/examples/order_to_cash/"
        "test_order_to_cash_statecharts.py"
    )
    restart = (
        "tests/e2e/sose/examples/order_to_cash/"
        "test_order_to_cash_restart_equivalence.py"
    )
    process_equivalence = (
        "tests/e2e/sose/examples/order_to_cash/"
        "test_order_to_cash_process_equivalence.py"
    )
    recurring = "tests/e2e/sose/jobs/test_recurring_reference_complete.py"

    evidence_sources: dict[ProcessEvidence, tuple[str, ...]] = {
        ProcessEvidence.ENTITIES: (entities, statechart_test),
        ProcessEvidence.STATECHARTS: (statecharts, statechart_test),
        ProcessEvidence.COMMAND_EVENT_PATH: (simulation, runtime),
        ProcessEvidence.HAPPY_PATH: (runtime,),
        ProcessEvidence.E2E_TERMINAL_OUTCOME: (runtime, recurring),
        ProcessEvidence.SAD_PATHS: (runtime, scenarios),
        ProcessEvidence.FINITE_RESOURCES: (simulation, restart),
        ProcessEvidence.CAPACITY_CONTENTION: (restart,),
        ProcessEvidence.TIME_SEMANTICS: (simulation, runtime, restart),
        ProcessEvidence.DURABLE_STATE: (simulation, restart, process_equivalence),
        ProcessEvidence.RESTART_EQUIVALENCE: (restart, process_equivalence),
        ProcessEvidence.REPLAY_IDEMPOTENCE: (simulation, runtime, process_equivalence),
        ProcessEvidence.RECURRING_RECONCILIATION: (definition, recurring),
        ProcessEvidence.FAULT_RECOVERY: (runtime, restart),
        ProcessEvidence.KPIS: (observability, observability_test, specification),
        ProcessEvidence.ERD: (specification,),
        ProcessEvidence.STATECHART_DOCUMENTATION: (statecharts, statechart_test, specification),
        ProcessEvidence.PROCESS_DIAGRAM: (specification,),
        ProcessEvidence.PROJECTION_CONTRACT: (
            observability,
            observability_test,
            specification,
        ),
        ProcessEvidence.CONFIGURATION_DOCUMENTATION: (
            config,
            definition,
            specification,
        ),
    }

    return ProcessManifest(
        domain="order_to_cash",
        evidence=frozenset(evidence),
        trigger="sales_order_submitted",
        terminal_outcomes=frozenset({"collected"}),
        resources=frozenset({"fulfillment_team", "collection_agent"}),
        sad_paths=frozenset(
            {
                "credit_hold",
                "fulfillment_capacity_loss",
                "fulfillment_contention",
                "partial_fulfillment",
                "overdue_collection",
            }
        ),
        kpis=frozenset(
            {
                "cash_collection",
                "order_to_cash_seconds",
                "amount",
                "transition_count",
                "credit_hold_count",
                "partial_fulfillment_count",
                "overdue_count",
                "collection_case_count",
                "collection_escalation_count",
            }
        ),
        specification_path=specification,
        evidence_sources=evidence_sources,
        assessment_complete=True,
    )

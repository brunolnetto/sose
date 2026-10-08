from __future__ import annotations

from sose.examples.process_manifest import (
    PROCESS_MATURITY_REQUIREMENTS,
    ProcessEvidence,
    ProcessManifest,
    ProcessMaturity,
)


def process_manifest() -> ProcessManifest:
    """Return the reviewed process-canonical evidence for Procure-to-Pay."""

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

    simulation = "src/sose/examples/p2p/simulation.py"
    entities = "src/sose/examples/p2p/entities.py"
    statecharts = "src/sose/examples/p2p/statecharts.py"
    definition = "src/sose/examples/p2p/definition.py"
    specification = "docs/examples/procure-to-pay/specification.md"
    observability = "src/sose/examples/p2p/observability.py"
    config = "src/sose/examples/p2p/config.py"
    observability_test = (
        "tests/unit/sose/examples/p2p/test_p2p_observability.py"
    )
    happy_path = "tests/integration/sose/examples/p2p/test_p2p_happy_path.py"
    receiving = "tests/integration/sose/examples/p2p/test_p2p_receiving_resources.py"
    exceptions = "tests/integration/sose/examples/p2p/test_p2p_receipt_exceptions.py"
    exception_restart = (
        "tests/integration/sose/examples/p2p/"
        "test_p2p_receipt_exception_restart.py"
    )
    replay = "tests/integration/sose/examples/p2p/test_p2p_replay_idempotence.py"
    shortage = "tests/integration/sose/examples/p2p/test_p2p_shortage_backorder.py"
    restart = "tests/e2e/sose/examples/p2p/test_p2p_restart_equivalence.py"
    recurring = "tests/e2e/sose/jobs/test_recurring_reference_complete.py"

    evidence_sources: dict[ProcessEvidence, tuple[str, ...]] = {
        ProcessEvidence.ENTITIES: (entities, happy_path),
        ProcessEvidence.STATECHARTS: (statecharts, specification),
        ProcessEvidence.COMMAND_EVENT_PATH: (simulation, happy_path),
        ProcessEvidence.HAPPY_PATH: (happy_path,),
        ProcessEvidence.E2E_TERMINAL_OUTCOME: (happy_path,),
        ProcessEvidence.SAD_PATHS: (receiving, exceptions, shortage),
        ProcessEvidence.FINITE_RESOURCES: (simulation, receiving, specification),
        ProcessEvidence.CAPACITY_CONTENTION: (receiving, specification),
        ProcessEvidence.TIME_SEMANTICS: (simulation, happy_path, specification),
        ProcessEvidence.DURABLE_STATE: (simulation, restart, exception_restart),
        ProcessEvidence.RESTART_EQUIVALENCE: (restart, exception_restart),
        ProcessEvidence.REPLAY_IDEMPOTENCE: (simulation, replay),
        ProcessEvidence.RECURRING_RECONCILIATION: (definition, recurring),
        ProcessEvidence.FAULT_RECOVERY: (happy_path, exception_restart, restart),
        ProcessEvidence.KPIS: (observability, observability_test, specification),
        ProcessEvidence.ERD: (specification,),
        ProcessEvidence.STATECHART_DOCUMENTATION: (
            statecharts,
            observability_test,
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
        domain="p2p",
        evidence=frozenset(evidence),
        trigger="requisition_requested",
        terminal_outcomes=frozenset({"consumed"}),
        resources=frozenset({"receiving_dock", "inspector"}),
        sad_paths=frozenset(
            {
                "receiving_contention",
                "shortage_backorder",
                "partial_receipt",
                "rejected_receipt",
            }
        ),
        kpis=frozenset(
            {
                "procure_to_consumption_seconds",
                "quantity",
                "transition_count",
                "supplier_delay_count",
                "partial_receipt_count",
                "rejected_receipt_count",
                "backorder_count",
                "consumed",
            }
        ),
        specification_path=specification,
        evidence_sources=evidence_sources,
        assessment_complete=True,
    )

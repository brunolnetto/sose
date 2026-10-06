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
    evidence.update(
        {
            ProcessEvidence.ERD,
            ProcessEvidence.PROCESS_DIAGRAM,
        }
    )

    simulation = "src/sose/examples/p2p/simulation.py"
    entities = "src/sose/examples/p2p/entities.py"
    statecharts = "src/sose/examples/p2p/statecharts.py"
    definition = "src/sose/examples/p2p/definition.py"
    specification = "docs/examples/procure-to-pay/specification.md"
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
        ProcessEvidence.ERD: (specification,),
        ProcessEvidence.PROCESS_DIAGRAM: (specification,),
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
        specification_path=specification,
        evidence_sources=evidence_sources,
        assessment_complete=True,
    )

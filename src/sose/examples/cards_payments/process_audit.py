from __future__ import annotations

from sose.examples.process_manifest import (
    PROCESS_MATURITY_REQUIREMENTS,
    ProcessEvidence,
    ProcessManifest,
    ProcessMaturity,
)


def process_manifest() -> ProcessManifest:
    """Return the reviewed process-canonical evidence for Cards & Payments."""

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
            ProcessEvidence.STATECHART_DOCUMENTATION,
        }
    )

    simulation = "src/sose/examples/cards_payments/simulation.py"
    entities = "src/sose/examples/cards_payments/entities.py"
    statecharts = "src/sose/examples/cards_payments/statecharts.py"
    definition = "src/sose/examples/cards_payments/definition.py"
    specification = "docs/examples/cards-payments/specification.md"
    happy_paths = (
        "tests/integration/sose/examples/cards_payments/"
        "test_cards_payments_happy_paths.py"
    )
    reversal = (
        "tests/integration/sose/examples/cards_payments/"
        "test_cards_payments_reversal.py"
    )
    statechart_test = (
        "tests/integration/sose/examples/cards_payments/"
        "test_cards_payments_statecharts.py"
    )
    scenarios = (
        "tests/integration/sose/examples/cards_payments/"
        "test_cards_payments_scenarios.py"
    )
    edges = (
        "tests/integration/sose/examples/cards_payments/"
        "test_cards_payments_simulation_edges.py"
    )
    restart = (
        "tests/e2e/sose/examples/cards_payments/"
        "test_cards_payments_restart_equivalence.py"
    )
    recurring = "tests/unit/sose/jobs/test_recurring_domain_reconciliation.py"

    evidence_sources: dict[ProcessEvidence, tuple[str, ...]] = {
        ProcessEvidence.ENTITIES: (entities, happy_paths),
        ProcessEvidence.STATECHARTS: (statecharts, statechart_test, specification),
        ProcessEvidence.COMMAND_EVENT_PATH: (simulation, happy_paths),
        ProcessEvidence.HAPPY_PATH: (happy_paths,),
        ProcessEvidence.E2E_TERMINAL_OUTCOME: (happy_paths, reversal),
        ProcessEvidence.SAD_PATHS: (happy_paths, reversal, scenarios, edges),
        ProcessEvidence.FINITE_RESOURCES: (simulation, edges, specification),
        ProcessEvidence.CAPACITY_CONTENTION: (edges,),
        ProcessEvidence.TIME_SEMANTICS: (simulation, restart, edges, specification),
        ProcessEvidence.DURABLE_STATE: (simulation, restart),
        ProcessEvidence.RESTART_EQUIVALENCE: (restart,),
        ProcessEvidence.REPLAY_IDEMPOTENCE: (simulation, edges),
        ProcessEvidence.RECURRING_RECONCILIATION: (definition, recurring),
        ProcessEvidence.FAULT_RECOVERY: (restart, scenarios),
        ProcessEvidence.ERD: (specification,),
        ProcessEvidence.STATECHART_DOCUMENTATION: (statecharts, specification),
    }

    return ProcessManifest(
        domain="cards_payments",
        evidence=frozenset(evidence),
        trigger="payment_authorization_requested",
        terminal_outcomes=frozenset({"declined", "reversed", "refunded"}),
        resources=frozenset(
            {"authorization_processor", "settlement_processor", "dispute_analyst"}
        ),
        sad_paths=frozenset(
            {
                "authorization_decline",
                "authorization_reversal",
                "settlement_retry",
                "processor_outage",
                "post_settlement_refund",
                "dispute_chargeback",
            }
        ),
        specification_path=specification,
        evidence_sources=evidence_sources,
        assessment_complete=True,
    )

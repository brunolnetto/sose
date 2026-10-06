from __future__ import annotations

from sose.examples.process_manifest import (
    PROCESS_MATURITY_REQUIREMENTS,
    ProcessEvidence,
    ProcessManifest,
    ProcessMaturity,
)


def process_manifest() -> ProcessManifest:
    """Return the reviewed process-canonical evidence for Logistics."""

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

    simulation = "src/sose/examples/logistics/simulation.py"
    entities = "src/sose/examples/logistics/entities.py"
    statecharts = "src/sose/examples/logistics/statecharts.py"
    definition = "src/sose/examples/logistics/definition.py"
    specification = "docs/examples/logistics/specification.md"
    happy_path = "tests/integration/sose/examples/logistics/test_logistics_happy_path.py"
    statechart_test = "tests/integration/sose/examples/logistics/test_logistics_statecharts.py"
    contention = "tests/integration/sose/examples/logistics/test_logistics_contention.py"
    scenarios = "tests/integration/sose/examples/logistics/test_logistics_scenarios.py"
    restart = "tests/e2e/sose/examples/logistics/test_logistics_restart_equivalence.py"
    edge_tests = "tests/unit/sose/examples/logistics/test_coverage_logistics_edges.py"
    recurring = "tests/unit/sose/jobs/test_recurring_domain_reconciliation.py"

    evidence_sources: dict[ProcessEvidence, tuple[str, ...]] = {
        ProcessEvidence.ENTITIES: (entities, happy_path),
        ProcessEvidence.STATECHARTS: (statecharts, statechart_test, specification),
        ProcessEvidence.COMMAND_EVENT_PATH: (simulation, happy_path),
        ProcessEvidence.HAPPY_PATH: (happy_path,),
        ProcessEvidence.E2E_TERMINAL_OUTCOME: (happy_path,),
        ProcessEvidence.SAD_PATHS: (happy_path, contention, scenarios, edge_tests),
        ProcessEvidence.FINITE_RESOURCES: (simulation, contention, edge_tests, specification),
        ProcessEvidence.CAPACITY_CONTENTION: (contention, edge_tests),
        ProcessEvidence.TIME_SEMANTICS: (simulation, restart, edge_tests, specification),
        ProcessEvidence.DURABLE_STATE: (simulation, restart, contention),
        ProcessEvidence.RESTART_EQUIVALENCE: (restart,),
        ProcessEvidence.REPLAY_IDEMPOTENCE: (simulation, edge_tests),
        ProcessEvidence.RECURRING_RECONCILIATION: (definition, recurring),
        ProcessEvidence.FAULT_RECOVERY: (restart, scenarios),
        ProcessEvidence.ERD: (specification,),
        ProcessEvidence.STATECHART_DOCUMENTATION: (statecharts, specification),
    }

    return ProcessManifest(
        domain="logistics",
        evidence=frozenset(evidence),
        trigger="shipment_created",
        terminal_outcomes=frozenset({"delivered", "lost", "damaged", "returned"}),
        resources=frozenset(
            {
                "pickup_courier",
                "origin_dock",
                "transfer_vehicle",
                "destination_dock",
                "delivery_courier",
            }
        ),
        sad_paths=frozenset(
            {
                "hub_congestion",
                "failed_delivery_retry",
                "courier_capacity_loss",
                "weather_delay",
                "lost_damaged_returned",
            }
        ),
        specification_path=specification,
        evidence_sources=evidence_sources,
        assessment_complete=True,
    )

from __future__ import annotations

from sose.examples.process_manifest import (
    PROCESS_MATURITY_REQUIREMENTS,
    ProcessEvidence,
    ProcessManifest,
    ProcessMaturity,
)


def process_manifest() -> ProcessManifest:
    """Return the reviewed process-canonical evidence for Maintenance / MRO."""

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
            ProcessEvidence.PROCESS_DIAGRAM,
            ProcessEvidence.KPIS,
            ProcessEvidence.PROJECTION_CONTRACT,
            ProcessEvidence.CONFIGURATION_DOCUMENTATION,
        }
    )

    simulation = "src/sose/examples/mro/simulation.py"
    entities = "src/sose/examples/mro/entities.py"
    statecharts = "src/sose/examples/mro/statecharts.py"
    definition = "src/sose/examples/mro/definition.py"
    specification = "docs/examples/mro/specification.md"
    happy_path = (
        "tests/integration/sose/examples/mro/"
        "test_mro_reference_happy_path.py"
    )
    probabilistic = (
        "tests/integration/sose/examples/mro/"
        "test_mro_probabilistic_gating.py"
    )
    sad_paths = "tests/integration/sose/examples/mro/test_mro_sad_paths.py"
    scenarios = "tests/integration/sose/examples/mro/test_mro_scenarios.py"
    review_regressions = (
        "tests/integration/sose/examples/mro/"
        "test_mro_review_regressions.py"
    )
    restart = "tests/e2e/sose/examples/mro/test_mro_restart_equivalence.py"
    emergency = (
        "tests/unit/sose/examples/mro/"
        "test_mro_emergency_preemption.py"
    )
    edge_tests = "tests/unit/sose/examples/mro/test_coverage_mro_edges.py"
    emergency_edges = (
        "tests/unit/sose/examples/mro/"
        "test_coverage_mro_emergency_edges.py"
    )
    warehouse_restart = (
        "tests/unit/sose/examples/mro/"
        "test_mro_domain_warehouse_restart.py"
    )
    recurring = (
        "tests/unit/sose/examples/mro/"
        "test_mro_declarative_recurring_e2e.py"
    )
    observability = "src/sose/examples/mro/observability.py"
    config = "src/sose/examples/mro/config.py"
    observability_test = (
        "tests/unit/sose/examples/mro/test_mro_observability.py"
    )

    evidence_sources: dict[ProcessEvidence, tuple[str, ...]] = {
        ProcessEvidence.ENTITIES: (entities, happy_path),
        ProcessEvidence.STATECHARTS: (
            statecharts,
            probabilistic,
            specification,
        ),
        ProcessEvidence.COMMAND_EVENT_PATH: (simulation, happy_path),
        ProcessEvidence.HAPPY_PATH: (happy_path,),
        ProcessEvidence.E2E_TERMINAL_OUTCOME: (happy_path,),
        ProcessEvidence.SAD_PATHS: (
            sad_paths,
            scenarios,
            emergency,
            review_regressions,
        ),
        ProcessEvidence.FINITE_RESOURCES: (
            simulation,
            sad_paths,
            specification,
        ),
        ProcessEvidence.CAPACITY_CONTENTION: (sad_paths,),
        ProcessEvidence.TIME_SEMANTICS: (
            simulation,
            restart,
            specification,
        ),
        ProcessEvidence.DURABLE_STATE: (
            simulation,
            restart,
            warehouse_restart,
        ),
        ProcessEvidence.RESTART_EQUIVALENCE: (
            restart,
            warehouse_restart,
        ),
        ProcessEvidence.REPLAY_IDEMPOTENCE: (
            simulation,
            review_regressions,
            edge_tests,
        ),
        ProcessEvidence.RECURRING_RECONCILIATION: (
            definition,
            recurring,
        ),
        ProcessEvidence.FAULT_RECOVERY: (
            restart,
            emergency,
            emergency_edges,
            scenarios,
        ),
        ProcessEvidence.ERD: (specification,),
        ProcessEvidence.STATECHART_DOCUMENTATION: (
            statecharts,
            specification,
        ),
        ProcessEvidence.PROCESS_DIAGRAM: (specification,),
        ProcessEvidence.KPIS: (observability, observability_test, specification),
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
        domain="mro",
        evidence=frozenset(evidence),
        trigger="maintenance_work_order_created",
        terminal_outcomes=frozenset({"closed", "cancelled"}),
        resources=frozenset(
            {"technician", "maintenance_bay", "spare_parts"}
        ),
        sad_paths=frozenset(
            {
                "spare_part_shortage",
                "technician_contention",
                "maintenance_bay_contention",
                "emergency_preemption",
                "cancellation",
            }
        ),
        kpis=frozenset(
            {
                "closure",
                "lead_time_seconds",
                "parts_consumed",
                "remaining_spare_parts",
                "transition_count",
                "material_wait_count",
                "resource_wait_count",
                "interruption_count",
            }
        ),
        specification_path=specification,
        evidence_sources=evidence_sources,
        assessment_complete=True,
    )

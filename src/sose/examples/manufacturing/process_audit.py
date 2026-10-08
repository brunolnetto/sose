from __future__ import annotations

from sose.examples.process_manifest import (
    PROCESS_MATURITY_REQUIREMENTS,
    ProcessEvidence,
    ProcessManifest,
    ProcessMaturity,
)


def process_manifest() -> ProcessManifest:
    """Return the reviewed process-canonical evidence for Manufacturing."""

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
        }
    )

    simulation = "src/sose/examples/manufacturing/simulation.py"
    entities = "src/sose/examples/manufacturing/entities.py"
    statecharts = "src/sose/examples/manufacturing/statecharts.py"
    definition = "src/sose/examples/manufacturing/definition.py"
    specification = "docs/examples/manufacturing/specification.md"
    happy_path = (
        "tests/integration/sose/examples/manufacturing/"
        "test_manufacturing_happy_path.py"
    )
    material_wip = (
        "tests/integration/sose/examples/manufacturing/"
        "test_manufacturing_material_wip.py"
    )
    resources = (
        "tests/integration/sose/examples/manufacturing/"
        "test_manufacturing_resources.py"
    )
    scenarios = (
        "tests/integration/sose/examples/manufacturing/"
        "test_manufacturing_scenarios.py"
    )
    restart = (
        "tests/e2e/sose/examples/manufacturing/"
        "test_manufacturing_restart_equivalence.py"
    )
    breakdown = (
        "tests/unit/sose/examples/manufacturing/"
        "test_manufacturing_breakdown.py"
    )
    quality_rework = (
        "tests/unit/sose/examples/manufacturing/"
        "test_manufacturing_quality_rework.py"
    )
    rework_restart = (
        "tests/unit/sose/examples/manufacturing/"
        "test_manufacturing_rework_restart.py"
    )
    edge_tests = (
        "tests/unit/sose/examples/manufacturing/"
        "test_coverage_manufacturing_edges.py"
    )
    recurring = "tests/e2e/sose/jobs/test_recurring_reference_complete.py"

    evidence_sources: dict[ProcessEvidence, tuple[str, ...]] = {
        ProcessEvidence.ENTITIES: (entities, happy_path),
        ProcessEvidence.STATECHARTS: (statecharts, happy_path, specification),
        ProcessEvidence.COMMAND_EVENT_PATH: (simulation, happy_path),
        ProcessEvidence.HAPPY_PATH: (happy_path,),
        ProcessEvidence.E2E_TERMINAL_OUTCOME: (happy_path, recurring),
        ProcessEvidence.SAD_PATHS: (
            material_wip,
            breakdown,
            quality_rework,
            scenarios,
        ),
        ProcessEvidence.FINITE_RESOURCES: (simulation, resources, specification),
        ProcessEvidence.CAPACITY_CONTENTION: (resources,),
        ProcessEvidence.TIME_SEMANTICS: (simulation, restart, specification),
        ProcessEvidence.DURABLE_STATE: (
            simulation,
            restart,
            rework_restart,
        ),
        ProcessEvidence.RESTART_EQUIVALENCE: (restart, rework_restart),
        ProcessEvidence.REPLAY_IDEMPOTENCE: (
            simulation,
            rework_restart,
            edge_tests,
        ),
        ProcessEvidence.RECURRING_RECONCILIATION: (definition, recurring),
        ProcessEvidence.FAULT_RECOVERY: (
            restart,
            breakdown,
            scenarios,
        ),
        ProcessEvidence.ERD: (specification,),
        ProcessEvidence.STATECHART_DOCUMENTATION: (statecharts, specification),
        ProcessEvidence.PROCESS_DIAGRAM: (specification,),
    }

    return ProcessManifest(
        domain="manufacturing",
        evidence=frozenset(evidence),
        trigger="production_order_created",
        terminal_outcomes=frozenset({"completed", "cancelled"}),
        resources=frozenset({"machine", "operator"}),
        sad_paths=frozenset(
            {
                "material_shortage",
                "capacity_contention",
                "machine_breakdown",
                "quality_hold_rework",
            }
        ),
        specification_path=specification,
        evidence_sources=evidence_sources,
        assessment_complete=True,
    )

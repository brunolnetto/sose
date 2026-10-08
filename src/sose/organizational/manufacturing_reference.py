from __future__ import annotations

from functools import cached_property
from typing import Final

from pydantic import BaseModel, ConfigDict, Field

from sose.backends.simpy import SimPyBackend
from sose.core.randomness import scoped_seed
from sose.examples.manufacturing.observability import (
    manufacturing_kpis,
    manufacturing_projection,
)
from sose.examples.manufacturing.scenarios import (
    machine_downtime_scenario,
    yield_degradation_scenario,
)
from sose.examples.manufacturing.simulation import (
    ORIGIN,
    build_runtime,
    reconcile_material_issue,
    reconcile_output,
    reconcile_repair,
    reconcile_scenario_breakdown,
    reconcile_setup_resources,
    release_setup_resources,
    seed_happy_path,
    seed_material,
)
from sose.persistence.memory import MemoryPersistence

from .agency import AgencyLevel, AgencySpec
from .domain_experiment import AgencyConfiguration, ExperimentWorld
from .domain_experiment_analysis import ComparisonContext, ComparisonEligibility
from .domain_experiment_runtime import (
    DomainExecutionRequest,
    DomainExecutionResult,
    DomainExperimentPlan,
    ExperimentObservation,
    RegimeReference,
    evidence_hash,
)
from .domain_reference import (
    AgencyCapabilitySpec,
    DomainReferenceDescriptor,
    DomainReferenceIdentity,
    GroundTruthClaim,
    GroundTruthComparisonKind,
    GroundTruthComparisonRule,
    GroundTruthKind,
    GroundTruthTargetKind,
    ParameterDefinition,
)
from .experiment import (
    ExperimentProtocol,
    FalsificationRule,
    MetricDirection,
    MultipleComparisonMethod,
    OutcomeMetric,
    ParameterRange,
    ReplicationPlan,
    SamplingDesign,
    StatisticalPlan,
)
from .model_spec import (
    EvidenceClass,
    InterventionClass,
    ModelIntervention,
    ModelSpec,
)


_QUANTITY_MIN: Final = 1.0
_QUANTITY_MAX: Final = 1000.0
_DEFAULT_QUANTITY: Final = 10.0
_BASE_YIELD: Final = 1.0
_DEGRADED_YIELD: Final = 0.8
_DOWNTIME_HOURS: Final = 3.0


class ManufacturingExperimentEvidence(BaseModel):
    """Compact deterministic evidence projected from durable Manufacturing truth."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    arm_id: str
    replication: int = Field(ge=0)
    quantity: float = Field(gt=0.0, allow_inf_nan=False)
    order_state: str
    operation_state: str
    completed: bool
    lead_time_seconds: float = Field(ge=0.0, allow_inf_nan=False)
    output_quantity: float = Field(ge=0.0, allow_inf_nan=False)
    yield_ratio: float = Field(ge=0.0, le=1.0, allow_inf_nan=False)
    transition_count: int = Field(ge=0)
    rework_count: int = Field(ge=0)
    breakdown_count: int = Field(ge=0)
    raw_material_quantity: float = Field(ge=0.0, allow_inf_nan=False)
    finished_goods_quantity: float = Field(ge=0.0, allow_inf_nan=False)
    wip_item_count: int = Field(ge=0)
    material_issue_count: int = Field(ge=0)
    preemption_count: int = Field(ge=0)
    machine_reacquired: bool


class ManufacturingReferenceDomain:
    """Gate-A adapter for the PC5 Manufacturing process canonical.

    The adapter contributes Manufacturing semantics only. DOE sampling, canonical
    worlds, CRN groups, replication orchestration, and reporting remain framework-owned.
    """

    @cached_property
    def descriptor(self) -> DomainReferenceDescriptor:
        return DomainReferenceDescriptor(
            identity=DomainReferenceIdentity(
                domain="manufacturing",
                reference_id="manufacturing-organizational-v1",
                reference_version="1",
                process_manifest_domain="manufacturing",
                specification_path="docs/examples/manufacturing/specification.md",
            ),
            parameters=(
                ParameterDefinition(
                    name="quantity",
                    description="Configured planned production quantity.",
                    units="units",
                    default=_DEFAULT_QUANTITY,
                    range=ParameterRange(low=_QUANTITY_MIN, high=_QUANTITY_MAX),
                    evidence_class=EvidenceClass.ASSUMED,
                ),
            ),
            agency_capabilities=(
                AgencyCapabilitySpec(
                    level=AgencyLevel.A0,
                    capability_id="fixed",
                ),
            ),
            ground_truth_kinds=(
                GroundTruthKind.INVARIANT,
                GroundTruthKind.MECHANISTIC,
            ),
        )

    def build_model(self, point: dict[str, float]) -> ModelSpec:
        if set(point) != {"quantity"}:
            raise ValueError("Manufacturing v1 requires exactly the quantity parameter")
        quantity = float(point["quantity"])
        if not _QUANTITY_MIN <= quantity <= _QUANTITY_MAX:
            raise ValueError("Manufacturing quantity is outside the v1 experiment range")
        return ModelSpec(
            stations={"work_center": "wc-10"},
            actors={"operator": {"capacity": 1.0}},
            capabilities={"machine": {"capacity": 1.0, "preemptive": True}},
            routing={"operations": 1},
            quality_gates={"inspection": {"required": True}},
            policies={
                "material_issue": "exactly_once",
                "wip_release": "quality_gated",
            },
            demand={"production_order_count": 1},
            agency=AgencySpec(level=AgencyLevel.A0),
            parameters={
                "quantity": quantity,
                "yield_factor": _BASE_YIELD,
                "machine_downtime_hours": 0.0,
            },
            parameter_evidence={
                "quantity": EvidenceClass.ASSUMED,
                "yield_factor": EvidenceClass.ASSUMED,
                "machine_downtime_hours": EvidenceClass.ASSUMED,
            },
        )

    def interventions(self) -> tuple[ModelIntervention, ...]:
        return (
            ModelIntervention(
                intervention_id="machine_downtime",
                intervention_class=InterventionClass.CAPACITY,
                set_values={
                    "/parameters/machine_downtime_hours": _DOWNTIME_HOURS,
                },
                mechanisms_changed=["machine availability"],
                transition_time=0.0,
            ),
            ModelIntervention(
                intervention_id="yield_degradation",
                intervention_class=InterventionClass.POLICY,
                set_values={
                    "/parameters/yield_factor": _DEGRADED_YIELD,
                },
                mechanisms_changed=["production yield"],
                transition_time=0.0,
            ),
        )

    def execute(self, request: DomainExecutionRequest) -> DomainExecutionResult:
        world = request.world
        if world.agency_configuration.level is not AgencyLevel.A0:
            raise ValueError("Manufacturing v1 supports A0 only")
        if world.agency_configuration.capability_id != "fixed":
            raise ValueError("Manufacturing v1 supports the fixed A0 capability only")

        quantity = float(world.model_spec.parameters["quantity"])
        yield_factor = float(world.model_spec.parameters["yield_factor"])
        downtime_hours = float(world.model_spec.parameters["machine_downtime_hours"])
        scenarios = _scenarios_for_world(
            yield_factor=yield_factor,
            downtime_hours=downtime_hours,
        )
        if request.protocol.warmup != 0.0:
            raise ValueError("Manufacturing v1 requires zero warmup")
        required_horizon = max(1.0, downtime_hours)
        if request.protocol.horizon < required_horizon:
            raise ValueError(
                "Manufacturing protocol horizon is shorter than configured arm duration"
            )

        persistence = MemoryPersistence()
        entities = seed_happy_path(persistence, quantity=quantity)
        context, engine = build_runtime(
            persistence,
            scenarios=scenarios,
            random_seed=scoped_seed(
                request.root_seed,
                world.crn_group,
                request.replication,
            ),
        )
        backend = SimPyBackend(origin=ORIGIN)
        engine.rebuild_backend(backend)

        # The first tick activates ORIGIN scenarios and releases the scheduled
        # ProductionOrder / Operation commands exactly as the reference tests do.
        engine.advance_tick()
        seed_material(engine, backend, quantity=quantity)
        if not reconcile_setup_resources(
            persistence,
            engine,
            backend,
            entities=entities,
        ):
            raise RuntimeError("Manufacturing preflight capacity was not acquired")
        reconcile_material_issue(
            persistence,
            engine,
            backend,
            entities=entities,
            quantity=quantity,
        )

        machine_reacquired = False
        if downtime_hours > 0.0:
            if not reconcile_scenario_breakdown(
                persistence,
                engine,
                backend,
                entities=entities,
            ):
                raise RuntimeError("Manufacturing downtime did not produce durable breakdown")

            # Let the configured finite scenario expire before repair/resumption.
            for _ in range(8):
                if not context.scenarios.attribute("manufacturing.machine.down", False):
                    break
                engine.advance_tick()
                backend.run_until(context.clock.now)
            if context.scenarios.attribute("manufacturing.machine.down", False):
                raise RuntimeError("Manufacturing downtime scenario did not expire")

            if not reconcile_repair(
                persistence,
                engine,
                backend,
                entities=entities,
            ):
                raise RuntimeError("Manufacturing repair did not reacquire machine capacity")
            machine_reacquired = any(
                reservation.request_id == f"machine:{entities.production_order_id}"
                for reservation in persistence.preemptive_resource_reservations()
            )
            # Material issue is deliberately idempotent after repair.
            reconcile_material_issue(
                persistence,
                engine,
                backend,
                entities=entities,
                quantity=quantity,
            )

        reconcile_output(
            persistence,
            engine,
            backend,
            entities=entities,
            quantity=quantity,
        )

        projection = manufacturing_projection(persistence, entities=entities)
        kpis = manufacturing_kpis(persistence, entities=entities)
        material_issue_count = sum(
            result.request_id == "issue-raw-material-1"
            for result in persistence.container_operation_results()
        )
        preemption_count = len(persistence.resource_preemption_results())

        release_setup_resources(
            persistence,
            engine,
            backend,
            entities=entities,
        )

        if not projection.completed or projection.lead_time_seconds is None:
            raise RuntimeError("Manufacturing v1 official/preflight arms must complete")
        if kpis.yield_ratio is None:
            raise RuntimeError("Manufacturing v1 completed run requires finite yield ratio")

        evidence = ManufacturingExperimentEvidence(
            arm_id=world.arm_id,
            replication=request.replication,
            quantity=quantity,
            order_state=projection.order_state,
            operation_state=projection.operation_state,
            completed=projection.completed,
            lead_time_seconds=projection.lead_time_seconds,
            output_quantity=kpis.output_quantity,
            yield_ratio=kpis.yield_ratio,
            transition_count=kpis.transition_count,
            rework_count=kpis.rework_count,
            breakdown_count=kpis.breakdown_count,
            raw_material_quantity=projection.raw_material_quantity,
            finished_goods_quantity=projection.finished_goods_quantity,
            wip_item_count=projection.wip_item_count,
            material_issue_count=material_issue_count,
            preemption_count=preemption_count,
            machine_reacquired=machine_reacquired,
        )
        return DomainExecutionResult(
            evidence_hash=evidence_hash(evidence),
            evidence=evidence,
        )

    def observation(self, result: DomainExecutionResult) -> ExperimentObservation:
        evidence = result.evidence
        if not isinstance(evidence, ManufacturingExperimentEvidence):
            raise TypeError("Manufacturing reference received unsupported execution evidence")
        return ExperimentObservation(
            metrics={
                "completed": 1.0 if evidence.completed else 0.0,
                "lead_time_seconds": evidence.lead_time_seconds,
                "output_quantity": evidence.output_quantity,
                "yield_ratio": evidence.yield_ratio,
                "transition_count": float(evidence.transition_count),
                "rework_count": float(evidence.rework_count),
                "breakdown_count": float(evidence.breakdown_count),
                "raw_material_quantity": evidence.raw_material_quantity,
                "wip_item_count": float(evidence.wip_item_count),
                "material_issue_count": float(evidence.material_issue_count),
                "preemption_count": float(evidence.preemption_count),
                "machine_reacquired": 1.0 if evidence.machine_reacquired else 0.0,
            }
        )

    def ground_truth(self, world: ExperimentWorld) -> tuple[GroundTruthClaim, ...]:
        yield_factor = float(world.model_spec.parameters["yield_factor"])
        downtime_hours = float(world.model_spec.parameters["machine_downtime_hours"])
        expected_breakdowns = 1.0 if downtime_hours > 0.0 else 0.0
        provenance = (
            "docs/examples/manufacturing/specification.md",
            "src/sose/examples/manufacturing/simulation.py",
        )
        exact = GroundTruthComparisonRule(kind=GroundTruthComparisonKind.EXACT)
        return (
            GroundTruthClaim(
                claim_id="mfg.completed",
                kind=GroundTruthKind.INVARIANT,
                target_kind=GroundTruthTargetKind.METRIC,
                target_name="completed",
                expected=1.0,
                comparison_rule=exact,
                assumptions=("official v1 arms are terminal-completion paths",),
                eligibility_rule="all Manufacturing v1 worlds",
                eligible=True,
                provenance=provenance,
            ),
            GroundTruthClaim(
                claim_id="mfg.material-issued-once",
                kind=GroundTruthKind.INVARIANT,
                target_kind=GroundTruthTargetKind.METRIC,
                target_name="material_issue_count",
                expected=1.0,
                comparison_rule=exact,
                assumptions=("one ProductionOrder uses one original raw-material issue",),
                eligibility_rule="all Manufacturing v1 worlds",
                eligible=True,
                provenance=provenance,
            ),
            GroundTruthClaim(
                claim_id="mfg.raw-material-consumed",
                kind=GroundTruthKind.INVARIANT,
                target_kind=GroundTruthTargetKind.METRIC,
                target_name="raw_material_quantity",
                expected=0.0,
                comparison_rule=exact,
                assumptions=("seeded raw quantity equals planned quantity",),
                eligibility_rule="all completed Manufacturing v1 worlds",
                eligible=True,
                provenance=provenance,
            ),
            GroundTruthClaim(
                claim_id="mfg.wip-released",
                kind=GroundTruthKind.INVARIANT,
                target_kind=GroundTruthTargetKind.METRIC,
                target_name="wip_item_count",
                expected=0.0,
                comparison_rule=exact,
                assumptions=("successful quality release consumes durable WIP",),
                eligibility_rule="all completed Manufacturing v1 worlds",
                eligible=True,
                provenance=provenance,
            ),
            GroundTruthClaim(
                claim_id="mfg.yield-ratio",
                kind=GroundTruthKind.MECHANISTIC,
                target_kind=GroundTruthTargetKind.METRIC,
                target_name="yield_ratio",
                expected=yield_factor,
                comparison_rule=exact,
                assumptions=("configured yield factor directly scales durable WIP/output",),
                eligibility_rule="all completed Manufacturing v1 worlds",
                eligible=True,
                provenance=provenance,
            ),
            GroundTruthClaim(
                claim_id="mfg.breakdown-count",
                kind=GroundTruthKind.MECHANISTIC,
                target_kind=GroundTruthTargetKind.METRIC,
                target_name="breakdown_count",
                expected=expected_breakdowns,
                comparison_rule=exact,
                assumptions=(
                    "machine_downtime_hours > 0 activates one finite downtime scenario",
                    "downtime is translated through the durable breakdown workflow",
                ),
                eligibility_rule="all Manufacturing v1 worlds",
                eligible=True,
                provenance=provenance,
            ),
        )

    def classify_regime(self, world: ExperimentWorld) -> RegimeReference:
        yield_factor = float(world.model_spec.parameters["yield_factor"])
        downtime_hours = float(world.model_spec.parameters["machine_downtime_hours"])
        if downtime_hours > 0.0:
            label = "downtime_recovered"
        elif yield_factor < 1.0:
            label = "yield_degraded"
        else:
            label = "nominal"
        return RegimeReference(
            label=label,
            stable=None,
            metadata={
                "quantity": float(world.model_spec.parameters["quantity"]),
                "yield_factor": yield_factor,
                "machine_downtime_hours": downtime_hours,
            },
        )

    def comparison_eligibility(
        self,
        context: ComparisonContext,
    ) -> ComparisonEligibility:
        if context.kind.value != "intervention":
            return ComparisonEligibility(
                eligible=False,
                basis=("Manufacturing v1 supports A0 intervention comparisons only",),
                reason="agency comparisons are outside Manufacturing v1",
            )

        if (
            context.treatment_world.arm_id == "yield_degradation"
            and context.metric_name in {"output_quantity", "yield_ratio"}
        ):
            return ComparisonEligibility(
                eligible=True,
                basis=(
                    "yield degradation is configured exogenously before execution",
                    "paired worlds share quantity and CRN group",
                ),
            )

        if (
            context.treatment_world.arm_id == "machine_downtime"
            and context.metric_name in {"breakdown_count", "lead_time_seconds"}
        ):
            return ComparisonEligibility(
                eligible=True,
                basis=(
                    "downtime is configured exogenously before execution",
                    "paired worlds share quantity and CRN group",
                ),
            )

        return ComparisonEligibility(
            eligible=False,
            basis=("metric/arm pair has no preregistered Manufacturing v1 comparison",),
            reason=(
                f"Manufacturing v1 does not claim {context.metric_name} effect "
                f"for {context.treatment_world.arm_id}"
            ),
        )

    def crn_signature(self, result: DomainExecutionResult) -> object:
        # Manufacturing v1 contains no stochastic latent mechanism. Empty latent
        # evidence is therefore the honest CRN signature and must match across arms.
        if not isinstance(result.evidence, ManufacturingExperimentEvidence):
            raise TypeError("Manufacturing reference received unsupported execution evidence")
        return ()


def build_manufacturing_preflight_plan_v1(
    reference: ManufacturingReferenceDomain | None = None,
) -> DomainExperimentPlan:
    """Build a small non-official plan used only for adapter/conformance preflight."""

    reference = reference or ManufacturingReferenceDomain()
    baseline = reference.build_model({"quantity": _DEFAULT_QUANTITY})
    protocol = ExperimentProtocol(
        protocol_version="1",
        research_question=(
            "Manufacturing adapter preflight: do the nominal, finite downtime, "
            "and yield-degradation mechanics satisfy the generic experiment contract?"
        ),
        baseline_model_spec_hash=baseline.model_spec_hash,
        intervention_ids=("machine_downtime", "yield_degradation"),
        agency_levels=(AgencyLevel.A0,),
        parameter_ranges={
            "quantity": ParameterRange(low=_QUANTITY_MIN, high=_QUANTITY_MAX),
        },
        sampling_design=SamplingDesign.LATIN_HYPERCUBE,
        sample_size=2,
        outcomes=(
            OutcomeMetric(
                name="output_quantity",
                direction=MetricDirection.MAXIMIZE,
                equivalence_margin=1e-9,
            ),
            OutcomeMetric(
                name="yield_ratio",
                direction=MetricDirection.MAXIMIZE,
                equivalence_margin=1e-9,
            ),
            OutcomeMetric(
                name="breakdown_count",
                direction=MetricDirection.MINIMIZE,
                equivalence_margin=1e-9,
            ),
            OutcomeMetric(
                name="lead_time_seconds",
                direction=MetricDirection.MINIMIZE,
                equivalence_margin=1e-9,
            ),
        ),
        statistical_plan=StatisticalPlan(
            confidence_level=0.95,
            multiple_comparison=MultipleComparisonMethod.HOLM,
        ),
        replication_plan=ReplicationPlan(
            min_replications=2,
            max_replications=2,
            target_ci_half_width=0.01,
        ),
        warmup=0.0,
        horizon=24.0,
        falsification=FalsificationRule(
            primary_metric="output_quantity",
            theta_fraction=0.5,
        ),
    )
    return DomainExperimentPlan(
        protocol=protocol,
        design_seed=20261008,
        root_seed=20261008,
        agency_configurations=(
            AgencyConfiguration(
                level=AgencyLevel.A0,
                capability_id="fixed",
            ),
        ),
    )


def _scenarios_for_world(
    *,
    yield_factor: float,
    downtime_hours: float,
) -> tuple[object, ...]:
    if yield_factor not in {_BASE_YIELD, _DEGRADED_YIELD}:
        raise ValueError("unsupported Manufacturing v1 yield factor")
    if downtime_hours not in {0.0, _DOWNTIME_HOURS}:
        raise ValueError("unsupported Manufacturing v1 downtime duration")
    if yield_factor < 1.0 and downtime_hours > 0.0:
        raise ValueError("Manufacturing v1 arms do not combine interventions")

    if downtime_hours > 0.0:
        return (machine_downtime_scenario(),)
    if yield_factor < 1.0:
        return (yield_degradation_scenario(),)
    return ()

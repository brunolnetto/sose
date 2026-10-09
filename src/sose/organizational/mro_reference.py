from __future__ import annotations

from functools import cached_property
from typing import Final

from pydantic import BaseModel, ConfigDict, Field

from sose.backends.simpy import SimPyBackend
from sose.core.randomness import scoped_seed
from sose.examples.mro.observability import mro_kpis, mro_projection
from sose.examples.mro.simulation import (
    ORIGIN,
    PART_CAPACITY,
    build_runtime,
    reconcile_complete,
    reconcile_emergency_interrupt,
    reconcile_emergency_resume,
    reconcile_start,
    release_capacity,
    seed_reference,
    seed_spare_parts,
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
from .model_spec import EvidenceClass, InterventionClass, ModelIntervention, ModelSpec


_QUANTITY_MIN: Final = 1.0
_QUANTITY_MAX: Final = 20.0
_DEFAULT_QUANTITY: Final = 2.0


class MROExperimentEvidence(BaseModel):
    """Finite MRO preflight evidence derived from the durable operational store."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    arm_id: str
    replication: int = Field(ge=0)
    quantity: float = Field(gt=0.0, le=PART_CAPACITY, allow_inf_nan=False)
    work_order_state: str
    part_demand_state: str
    closed: bool
    lead_time_seconds: float = Field(ge=0.0, allow_inf_nan=False)
    parts_consumed: float = Field(ge=0.0, allow_inf_nan=False)
    remaining_spare_parts: float = Field(ge=0.0, allow_inf_nan=False)
    spare_part_issue_count: int = Field(ge=0)
    material_wait_count: int = Field(ge=0)
    resource_wait_count: int = Field(ge=0)
    interruption_count: int = Field(ge=0)
    preemption_count: int = Field(ge=0)
    transition_count: int = Field(ge=0)


class MROReferenceDomain:
    """Provisional PC5 MRO Gate-A adapter; the generic framework owns all worlds."""

    @cached_property
    def descriptor(self) -> DomainReferenceDescriptor:
        return DomainReferenceDescriptor(
            identity=DomainReferenceIdentity(
                domain="mro",
                reference_id="mro-organizational-preflight-v1",
                reference_version="1",
                process_manifest_domain="mro",
                specification_path="docs/examples/mro/specification.md",
            ),
            parameters=(
                ParameterDefinition(
                    name="quantity",
                    description="Exogenously configured spare-part demand quantity",
                    units="parts",
                    default=_DEFAULT_QUANTITY,
                    range=ParameterRange(low=_QUANTITY_MIN, high=_QUANTITY_MAX),
                    evidence_class=EvidenceClass.ASSUMED,
                ),
            ),
            agency_capabilities=(
                AgencyCapabilitySpec(level=AgencyLevel.A0, capability_id="fixed"),
            ),
            ground_truth_kinds=(GroundTruthKind.INVARIANT, GroundTruthKind.MECHANISTIC),
        )

    def build_model(self, point: dict[str, float]) -> ModelSpec:
        if set(point) != {"quantity"}:
            raise ValueError("MRO preflight requires configured quantity only")
        quantity = float(point["quantity"])
        if not _QUANTITY_MIN <= quantity <= _QUANTITY_MAX:
            raise ValueError("MRO preflight quantity is out of bounds")
        return ModelSpec(
            stations={"maintenance": "maintenance_bay"},
            actors={"technician": {"capacity": 1.0}},
            capabilities={"maintenance_bay": {"capacity": 1.0, "preemptive": True}},
            routing={"work_order": "planned_to_closed", "part_demand": "open_to_consumed"},
            policies={"part_issue": "exactly_once", "capacity_release": "on_completion"},
            demand={"work_order_count": 1},
            agency=AgencySpec(level=AgencyLevel.A0),
            parameters={
                "quantity": quantity,
                "spare_part_shortage": 0.0,
                "emergency_preemption": 0.0,
            },
            parameter_evidence={
                "quantity": EvidenceClass.ASSUMED,
                "spare_part_shortage": EvidenceClass.ASSUMED,
                "emergency_preemption": EvidenceClass.ASSUMED,
            },
        )

    def interventions(self) -> tuple[ModelIntervention, ...]:
        return (
            ModelIntervention(
                intervention_id="spare_part_shortage",
                intervention_class=InterventionClass.POLICY,
                set_values={"/parameters/spare_part_shortage": 1.0},
                mechanisms_changed=["initial inventory availability and material wait"],
            ),
            ModelIntervention(
                intervention_id="emergency_preemption",
                intervention_class=InterventionClass.CAPACITY,
                set_values={"/parameters/emergency_preemption": 1.0},
                mechanisms_changed=["finite maintenance-bay preemption and recovery"],
            ),
        )

    def execute(self, request: DomainExecutionRequest) -> DomainExecutionResult:
        world = request.world
        if world.agency_configuration.level is not AgencyLevel.A0:
            raise ValueError("MRO preflight supports A0 only")
        if world.agency_configuration.capability_id != "fixed":
            raise ValueError("MRO preflight supports fixed A0 capability only")
        if request.protocol.warmup != 0.0 or request.protocol.horizon < 4.0:
            raise ValueError("MRO preflight requires zero warmup and finite horizon")

        quantity = float(world.model_spec.parameters["quantity"])
        shortage = float(world.model_spec.parameters["spare_part_shortage"])
        emergency = float(world.model_spec.parameters["emergency_preemption"])
        if shortage not in {0.0, 1.0} or emergency not in {0.0, 1.0} or (shortage and emergency):
            raise ValueError("MRO preflight interventions are mutually exclusive")

        persistence = MemoryPersistence()
        entities = seed_reference(persistence, quantity=quantity)
        _, engine = build_runtime(
            persistence,
            random_seed=scoped_seed(request.root_seed, world.crn_group, request.replication),
        )
        backend = SimPyBackend(origin=ORIGIN)
        engine.rebuild_backend(backend)
        backend.run_until(ORIGIN.replace(hour=9))

        if shortage:
            if reconcile_start(
                persistence, engine, backend, entities=entities, quantity=quantity,
            ):
                raise RuntimeError("MRO shortage arm unexpectedly started without inventory")
            state = persistence.entity("work_order", entities.work_order_id)
            if state is None or state.state != "waiting_material":
                raise RuntimeError("MRO shortage was not persisted as a material wait")
            # Advance the authoritative logical clock as well as the SimPy backend.
            # Backend-only time advancement would misstate persisted event timestamps.
            engine.advance_tick()
            backend.run_until(engine.context.clock.now)
            seed_spare_parts(engine, backend, quantity=quantity)
        else:
            seed_spare_parts(engine, backend, quantity=quantity)

        if not reconcile_start(
            persistence, engine, backend, entities=entities, quantity=quantity,
        ):
            raise RuntimeError("MRO preflight could not start after materials/capacity available")

        if emergency:
            if not reconcile_emergency_interrupt(persistence, engine, backend, entities=entities):
                raise RuntimeError("MRO emergency did not durably preempt maintenance capacity")
            if not reconcile_emergency_resume(persistence, engine, backend, entities=entities):
                raise RuntimeError("MRO emergency did not restore maintenance capacity")

        reconcile_complete(persistence, engine, entities=entities)
        release_capacity(persistence, engine, backend, entities=entities)
        projection = mro_projection(persistence, entities=entities)
        kpis = mro_kpis(persistence, entities=entities)
        if not projection.closed or projection.lead_time_seconds is None:
            raise RuntimeError("MRO preflight must reach a terminal closed work order")
        part_issues = sum(
            result.request_id == "consume-spare-part-1"
            for result in persistence.container_operation_results()
        )
        preemptions = len(persistence.resource_preemption_results())
        evidence = MROExperimentEvidence(
            arm_id=world.arm_id,
            replication=request.replication,
            quantity=quantity,
            work_order_state=projection.work_order_state,
            part_demand_state=projection.part_demand_state,
            closed=projection.closed,
            lead_time_seconds=projection.lead_time_seconds,
            parts_consumed=kpis.parts_consumed,
            remaining_spare_parts=kpis.remaining_spare_parts,
            spare_part_issue_count=part_issues,
            material_wait_count=kpis.material_wait_count,
            resource_wait_count=kpis.resource_wait_count,
            interruption_count=kpis.interruption_count,
            preemption_count=preemptions,
            transition_count=kpis.transition_count,
        )
        return DomainExecutionResult(evidence_hash=evidence_hash(evidence), evidence=evidence)

    def observation(self, result: DomainExecutionResult) -> ExperimentObservation:
        e = result.evidence
        if not isinstance(e, MROExperimentEvidence):
            raise TypeError("MRO evidence type mismatch")
        return ExperimentObservation(metrics={
            "closed": 1.0 if e.closed else 0.0,
            "quantity": e.quantity,
            "lead_time_seconds": e.lead_time_seconds,
            "parts_consumed": e.parts_consumed,
            "remaining_spare_parts": e.remaining_spare_parts,
            "spare_part_issue_count": float(e.spare_part_issue_count),
            "material_wait_count": float(e.material_wait_count),
            "resource_wait_count": float(e.resource_wait_count),
            "interruption_count": float(e.interruption_count),
            "preemption_count": float(e.preemption_count),
        })

    def ground_truth(self, world: ExperimentWorld) -> tuple[GroundTruthClaim, ...]:
        shortage = float(world.model_spec.parameters["spare_part_shortage"])
        emergency = float(world.model_spec.parameters["emergency_preemption"])
        quantity = float(world.model_spec.parameters["quantity"])
        expected = {
            "closed": (GroundTruthKind.INVARIANT, 1.0),
            "parts_consumed": (GroundTruthKind.INVARIANT, quantity),
            "remaining_spare_parts": (GroundTruthKind.INVARIANT, 0.0),
            "spare_part_issue_count": (GroundTruthKind.INVARIANT, 1.0),
            "material_wait_count": (GroundTruthKind.MECHANISTIC, shortage),
            "interruption_count": (GroundTruthKind.MECHANISTIC, emergency),
            "preemption_count": (GroundTruthKind.MECHANISTIC, emergency),
        }
        provenance = ("docs/examples/mro/specification.md", "src/sose/examples/mro/simulation.py")
        return tuple(
            GroundTruthClaim(
                claim_id=f"mro.{name}",
                kind=kind,
                target_kind=GroundTruthTargetKind.METRIC,
                target_name=name,
                expected=value,
                comparison_rule=GroundTruthComparisonRule(kind=GroundTruthComparisonKind.EXACT),
                assumptions=("one work order and one lot with fixed A0 policy",),
                eligibility_rule="all finite MRO preflight worlds",
                eligible=True,
                provenance=provenance,
            )
            for name, (kind, value) in expected.items()
        )

    def classify_regime(self, world: ExperimentWorld) -> RegimeReference:
        shortage = float(world.model_spec.parameters["spare_part_shortage"])
        emergency = float(world.model_spec.parameters["emergency_preemption"])
        return RegimeReference(
            label="shortage_recovered" if shortage else ("emergency_recovered" if emergency else "nominal"),
            stable=None,
            metadata={"quantity": float(world.model_spec.parameters["quantity"]),
                      "spare_part_shortage": shortage,
                      "emergency_preemption": emergency},
        )

    def comparison_eligibility(self, context: ComparisonContext) -> ComparisonEligibility:
        eligible = (
            context.kind.value == "intervention" and (
                (context.treatment_world.arm_id == "spare_part_shortage"
                 and context.metric_name in {"material_wait_count", "lead_time_seconds"})
                or (context.treatment_world.arm_id == "emergency_preemption"
                    and context.metric_name in {"interruption_count", "preemption_count"})
            )
        )
        return ComparisonEligibility(
            eligible=eligible,
            basis=("MRO exogenous shortage/preemption and paired CRN group",),
            reason=None if eligible else "no preregistered MRO preflight claim for this metric/arm",
        )

    def crn_signature(self, result: DomainExecutionResult) -> object:
        if not isinstance(result.evidence, MROExperimentEvidence):
            raise TypeError("MRO CRN signature requires typed evidence")
        return ()


def build_mro_preflight_plan_v1(reference: MROReferenceDomain | None = None) -> DomainExperimentPlan:
    """Small characterization experiment only; not a frozen official experiment."""
    reference = reference or MROReferenceDomain()
    baseline = reference.build_model({"quantity": _DEFAULT_QUANTITY})
    protocol = ExperimentProtocol(
        protocol_version="1",
        research_question="MRO preflight of finite inventory and emergency bay preemption",
        baseline_model_spec_hash=baseline.model_spec_hash,
        intervention_ids=("spare_part_shortage", "emergency_preemption"),
        agency_levels=(AgencyLevel.A0,),
        parameter_ranges={"quantity": ParameterRange(low=_QUANTITY_MIN, high=_QUANTITY_MAX)},
        sampling_design=SamplingDesign.LATIN_HYPERCUBE,
        sample_size=2,
        outcomes=(
            OutcomeMetric(name="lead_time_seconds", direction=MetricDirection.MINIMIZE, equivalence_margin=1.0),
            OutcomeMetric(name="parts_consumed", direction=MetricDirection.MAXIMIZE, equivalence_margin=1e-9),
            OutcomeMetric(name="material_wait_count", direction=MetricDirection.MINIMIZE, equivalence_margin=1e-9),
            OutcomeMetric(name="interruption_count", direction=MetricDirection.MINIMIZE, equivalence_margin=1e-9),
            OutcomeMetric(name="preemption_count", direction=MetricDirection.MINIMIZE, equivalence_margin=1e-9),
        ),
        statistical_plan=StatisticalPlan(confidence_level=0.95, multiple_comparison=MultipleComparisonMethod.HOLM),
        replication_plan=ReplicationPlan(min_replications=2, max_replications=2, target_ci_half_width=0.01),
        warmup=0.0,
        horizon=24.0,
        falsification=FalsificationRule(primary_metric="parts_consumed", theta_fraction=0.5),
        crn_enabled=True,
        report_null_regions=True,
    )
    return DomainExperimentPlan(
        protocol=protocol,
        design_seed=20261008,
        root_seed=20261008,
        agency_configurations=(AgencyConfiguration(level=AgencyLevel.A0, capability_id="fixed"),),
    )

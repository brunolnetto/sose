from __future__ import annotations

from functools import cached_property
from typing import Final

from pydantic import BaseModel, ConfigDict, Field

from sose.backends.simpy import SimPyBackend
from sose.core.randomness import scoped_seed
from sose.examples.order_to_cash.observability import order_to_cash_kpis, order_to_cash_projection
from sose.examples.order_to_cash.simulation import (
    ORIGIN,
    build_runtime,
    collect_receivable,
    reconcile_credit,
    reconcile_fulfillment,
    reconcile_collection,
    schedule_due,
    schedule_overdue,
    seed_reference,
    ship_invoice_and_ensure_receivable,
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


_AMOUNT_MIN: Final = 100.0
_AMOUNT_MAX: Final = 1000.0
_DEFAULT_AMOUNT: Final = 250.0


class O2CExperimentEvidence(BaseModel):
    """Evidence taken from persisted O2C entities and correlated events only."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    arm_id: str
    replication: int = Field(ge=0)
    amount: float = Field(gt=0, allow_inf_nan=False)
    order_state: str
    receivable_state: str
    collected: bool
    order_to_cash_seconds: float = Field(ge=0, allow_inf_nan=False)
    transition_count: int = Field(ge=0)
    partial_fulfillment_count: int = Field(ge=0)
    overdue_count: int = Field(ge=0)
    collection_case_count: int = Field(ge=0)
    collection_escalation_count: int = Field(ge=0)


class O2CReferenceDomain:
    """Provisional PC5 O2C Gate-A adapter, without domain-owned orchestration."""

    @cached_property
    def descriptor(self) -> DomainReferenceDescriptor:
        return DomainReferenceDescriptor(
            identity=DomainReferenceIdentity(
                domain="order_to_cash",
                reference_id="o2c-organizational-preflight-v1",
                reference_version="1",
                process_manifest_domain="order_to_cash",
                specification_path="docs/examples/order-to-cash/specification.md",
            ),
            parameters=(
                ParameterDefinition(
                    name="amount",
                    description="Configured monetary amount of the single sales order",
                    units="USD",
                    default=_DEFAULT_AMOUNT,
                    range=ParameterRange(low=_AMOUNT_MIN, high=_AMOUNT_MAX),
                    evidence_class=EvidenceClass.ASSUMED,
                ),
            ),
            agency_capabilities=(
                AgencyCapabilitySpec(level=AgencyLevel.A0, capability_id="fixed"),
            ),
            ground_truth_kinds=(GroundTruthKind.INVARIANT, GroundTruthKind.MECHANISTIC),
        )

    def build_model(self, point: dict[str, float]) -> ModelSpec:
        if set(point) != {"amount"}:
            raise ValueError("O2C preflight requires exactly one configured amount")
        amount = float(point["amount"])
        if not _AMOUNT_MIN <= amount <= _AMOUNT_MAX:
            raise ValueError("O2C preflight amount is out of bounds")
        return ModelSpec(
            stations={"fulfillment": "fulfillment_team", "collections": "collection_agent"},
            actors={"credit": {"policy": "approve"}},
            capabilities={"fulfillment_team": {"capacity": 1.0}, "collection_agent": {"capacity": 1.0}},
            routing={"sales_order_to_receivable": True},
            policies={"collection": "due_then_collect", "partial": "one_optional_partial_step"},
            demand={"sales_orders": 1},
            agency=AgencySpec(level=AgencyLevel.A0),
            parameters={
                "amount": amount,
                "partial_fulfillment": 0.0,
                "overdue_collection": 0.0,
            },
            parameter_evidence={
                "amount": EvidenceClass.ASSUMED,
                "partial_fulfillment": EvidenceClass.ASSUMED,
                "overdue_collection": EvidenceClass.ASSUMED,
            },
        )

    def interventions(self) -> tuple[ModelIntervention, ...]:
        return (
            ModelIntervention(
                intervention_id="partial_fulfillment",
                intervention_class=InterventionClass.POLICY,
                set_values={"/parameters/partial_fulfillment": 1.0},
                mechanisms_changed=["fulfillment handoff requires two durable stages"],
            ),
            ModelIntervention(
                intervention_id="overdue_collection",
                intervention_class=InterventionClass.POLICY,
                set_values={"/parameters/overdue_collection": 1.0},
                mechanisms_changed=["collection occurs after overdue escalation"],
            ),
        )

    def execute(self, request: DomainExecutionRequest) -> DomainExecutionResult:
        world = request.world
        if world.agency_configuration.level is not AgencyLevel.A0:
            raise ValueError("O2C preflight supports A0 only")
        if world.agency_configuration.capability_id != "fixed":
            raise ValueError("O2C preflight supports fixed policy only")
        if request.protocol.warmup != 0 or request.protocol.horizon < 6:
            raise ValueError("O2C preflight needs zero warmup and a six-hour horizon")

        amount = float(world.model_spec.parameters["amount"])
        partial = float(world.model_spec.parameters["partial_fulfillment"])
        overdue = float(world.model_spec.parameters["overdue_collection"])
        if partial not in {0.0, 1.0} or overdue not in {0.0, 1.0} or (partial and overdue):
            raise ValueError("O2C preflight interventions must not be combined")

        persistence = MemoryPersistence()
        entities = seed_reference(persistence, amount=amount)
        _, engine = build_runtime(
            persistence,
            random_seed=scoped_seed(request.root_seed, world.crn_group, request.replication),
        )
        backend = SimPyBackend(origin=ORIGIN)
        engine.rebuild_backend(backend)

        if not reconcile_credit(persistence, engine, entities=entities):
            raise RuntimeError("O2C credit approval was not durable")
        if partial:
            if reconcile_fulfillment(
                persistence, engine, backend, entities=entities, partial=True
            ):
                raise RuntimeError("partial fulfillment unexpectedly completed the order")
        if not reconcile_fulfillment(persistence, engine, backend, entities=entities):
            raise RuntimeError("O2C fulfillment failed")

        ship_invoice_and_ensure_receivable(persistence, engine, entities=entities)
        due_at = schedule_due(persistence, engine, backend, entities=entities)
        backend.run_until(due_at)
        if overdue:
            overdue_at = schedule_overdue(persistence, engine, backend, entities=entities)
            backend.run_until(overdue_at)
            if not reconcile_collection(persistence, engine, backend, entities=entities, promise=True):
                raise RuntimeError("O2C collection agent did not acquire finite capacity")
            scheduled = persistence.scheduled_work()
            if len(scheduled) != 1:
                raise RuntimeError("O2C promised collection must have one follow-up")
            backend.run_until(scheduled[0].due_at)

        if not collect_receivable(persistence, engine, entities=entities):
            raise RuntimeError("O2C receivable was not collected")
        projection = order_to_cash_projection(persistence, entities=entities)
        kpis = order_to_cash_kpis(persistence, entities=entities)
        if not projection.collected or projection.order_to_cash_seconds is None:
            raise RuntimeError("O2C preflight world failed to close")

        evidence = O2CExperimentEvidence(
            arm_id=world.arm_id,
            replication=request.replication,
            amount=projection.amount,
            order_state=projection.order_state,
            receivable_state=projection.receivable_state or "",
            collected=projection.collected,
            order_to_cash_seconds=projection.order_to_cash_seconds,
            transition_count=kpis.transition_count,
            partial_fulfillment_count=kpis.partial_fulfillment_count,
            overdue_count=kpis.overdue_count,
            collection_case_count=kpis.collection_case_count,
            collection_escalation_count=kpis.collection_escalation_count,
        )
        return DomainExecutionResult(evidence_hash=evidence_hash(evidence), evidence=evidence)

    def observation(self, result: DomainExecutionResult) -> ExperimentObservation:
        e = result.evidence
        if not isinstance(e, O2CExperimentEvidence):
            raise TypeError("O2C evidence type mismatch")
        return ExperimentObservation(metrics={
            "collected": 1.0 if e.collected else 0.0,
            "amount": e.amount,
            "order_to_cash_seconds": e.order_to_cash_seconds,
            "partial_fulfillment_count": float(e.partial_fulfillment_count),
            "overdue_count": float(e.overdue_count),
            "collection_case_count": float(e.collection_case_count),
            "collection_escalation_count": float(e.collection_escalation_count),
        })

    def ground_truth(self, world: ExperimentWorld) -> tuple[GroundTruthClaim, ...]:
        partial = float(world.model_spec.parameters["partial_fulfillment"])
        overdue = float(world.model_spec.parameters["overdue_collection"])
        provenance = (
            "docs/examples/order-to-cash/specification.md",
            "src/sose/examples/order_to_cash/simulation.py",
        )
        values = {
            "collected": (GroundTruthKind.INVARIANT, 1.0),
            "amount": (GroundTruthKind.INVARIANT, float(world.model_spec.parameters["amount"])),
            "partial_fulfillment_count": (GroundTruthKind.MECHANISTIC, partial),
            "overdue_count": (GroundTruthKind.MECHANISTIC, overdue),
            "collection_case_count": (GroundTruthKind.MECHANISTIC, overdue),
            "collection_escalation_count": (GroundTruthKind.MECHANISTIC, overdue),
        }
        return tuple(
            GroundTruthClaim(
                claim_id=f"o2c.{name}",
                kind=kind,
                target_kind=GroundTruthTargetKind.METRIC,
                target_name=name,
                expected=expected,
                comparison_rule=GroundTruthComparisonRule(kind=GroundTruthComparisonKind.EXACT),
                assumptions=("single deterministic configured order and fixed A0 policy",),
                eligibility_rule="all finite O2C preflight worlds",
                eligible=True,
                provenance=provenance,
            )
            for name, (kind, expected) in values.items()
        )

    def classify_regime(self, world: ExperimentWorld) -> RegimeReference:
        partial = float(world.model_spec.parameters["partial_fulfillment"])
        overdue = float(world.model_spec.parameters["overdue_collection"])
        return RegimeReference(
            label="partial" if partial else ("overdue" if overdue else "nominal"),
            stable=None,
            metadata={
                "amount": float(world.model_spec.parameters["amount"]),
                "partial_fulfillment": partial,
                "overdue_collection": overdue,
            },
        )

    def comparison_eligibility(self, context: ComparisonContext) -> ComparisonEligibility:
        eligible = (
            context.kind.value == "intervention" and (
                (context.treatment_world.arm_id == "partial_fulfillment"
                 and context.metric_name == "partial_fulfillment_count")
                or (context.treatment_world.arm_id == "overdue_collection"
                    and context.metric_name in {"overdue_count", "order_to_cash_seconds"})
            )
        )
        return ComparisonEligibility(
            eligible=eligible,
            basis=("configured O2C policy and paired exogenous amount",),
            reason=None if eligible else "no preregistered O2C preflight claim for metric/arm",
        )

    def crn_signature(self, result: DomainExecutionResult) -> object:
        if not isinstance(result.evidence, O2CExperimentEvidence):
            raise TypeError("O2C CRN signature requires typed evidence")
        return ()


def build_o2c_preflight_plan_v1(reference: O2CReferenceDomain | None = None) -> DomainExperimentPlan:
    """Small characterization experiment only; never official/frozen results."""
    reference = reference or O2CReferenceDomain()
    baseline = reference.build_model({"amount": _DEFAULT_AMOUNT})
    protocol = ExperimentProtocol(
        protocol_version="1",
        research_question="O2C preflight of administrative handoff and collection decisions",
        baseline_model_spec_hash=baseline.model_spec_hash,
        intervention_ids=("partial_fulfillment", "overdue_collection"),
        agency_levels=(AgencyLevel.A0,),
        parameter_ranges={"amount": ParameterRange(low=_AMOUNT_MIN, high=_AMOUNT_MAX)},
        sampling_design=SamplingDesign.LATIN_HYPERCUBE,
        sample_size=2,
        outcomes=(
            OutcomeMetric(name="order_to_cash_seconds", direction=MetricDirection.MINIMIZE, equivalence_margin=1.0),
            OutcomeMetric(name="partial_fulfillment_count", direction=MetricDirection.MINIMIZE, equivalence_margin=1e-9),
            OutcomeMetric(name="overdue_count", direction=MetricDirection.MINIMIZE, equivalence_margin=1e-9),
            OutcomeMetric(name="amount", direction=MetricDirection.MAXIMIZE, equivalence_margin=1e-9),
        ),
        statistical_plan=StatisticalPlan(confidence_level=0.95, multiple_comparison=MultipleComparisonMethod.HOLM),
        replication_plan=ReplicationPlan(min_replications=2, max_replications=2, target_ci_half_width=0.01),
        warmup=0.0,
        horizon=24.0,
        falsification=FalsificationRule(primary_metric="order_to_cash_seconds", theta_fraction=0.5),
        crn_enabled=True,
        report_null_regions=True,
    )
    return DomainExperimentPlan(
        protocol=protocol,
        design_seed=20261008,
        root_seed=20261008,
        agency_configurations=(AgencyConfiguration(level=AgencyLevel.A0, capability_id="fixed"),),
    )

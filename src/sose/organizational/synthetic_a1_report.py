from __future__ import annotations

from hashlib import sha256
import json
from statistics import fmean

from pydantic import BaseModel, ConfigDict, Field, InstanceOf

from .synthetic_a1_experiment import (
    A0A1ExperimentSummary,
    A0A1ReferenceExperimentResultV1,
    A1RegimeKind,
)


class A1InterventionEffectV1(BaseModel):
    """Paired intervention effect under A0 and A1 for one exogenous design point."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    design_index: int = Field(ge=0)
    arm_id: str
    replications: int = Field(ge=2)
    a0_delta_mean_lead_time: float = Field(allow_inf_nan=False)
    a1_delta_mean_lead_time: float = Field(allow_inf_nan=False)
    a0_delta_throughput: float = Field(allow_inf_nan=False)
    a1_delta_throughput: float = Field(allow_inf_nan=False)
    a0_delta_operating_cost: float = Field(allow_inf_nan=False)
    a1_delta_operating_cost: float = Field(allow_inf_nan=False)
    direction_eligible: bool
    a0_direction: int | None = Field(default=None, ge=-1, le=1)
    a1_direction: int | None = Field(default=None, ge=-1, le=1)
    direction_changed: bool | None = None
    agency_delta_intervention_effect: float = Field(allow_inf_nan=False)


class A1ScientificReportV1(BaseModel):
    """Scientific summary of agency-mediated synthetic intervention effects."""

    model_config = ConfigDict(extra="forbid", frozen=True, arbitrary_types_allowed=True)

    protocol_hash: str
    pair_count: int = Field(ge=1)
    regime_summary: InstanceOf[A0A1ExperimentSummary]
    intervention_effects: tuple[InstanceOf[A1InterventionEffectV1], ...]
    intervention_direction_eligible_count: int = Field(ge=0)
    intervention_direction_ineligible_count: int = Field(ge=0)
    intervention_direction_agreement_count: int = Field(ge=0)
    intervention_direction_change_count: int = Field(ge=0)
    intervention_direction_agreement_rate: float | None = Field(
        default=None,
        ge=0.0,
        le=1.0,
        allow_inf_nan=False,
    )
    mean_agency_delta_lead_time: float = Field(allow_inf_nan=False)
    mean_agency_delta_throughput: float = Field(allow_inf_nan=False)
    mean_agency_delta_operating_cost: float = Field(allow_inf_nan=False)
    mean_adaptation_actor_time: float = Field(ge=0.0, allow_inf_nan=False)
    mean_adaptation_cost: float = Field(ge=0.0, allow_inf_nan=False)

    def canonical_payload(self) -> dict[str, object]:
        return {
            "protocol_hash": self.protocol_hash,
            "pair_count": self.pair_count,
            "regime_summary": self.regime_summary.model_dump(mode="json"),
            "intervention_effects": [
                effect.model_dump(mode="json") for effect in self.intervention_effects
            ],
            "intervention_direction_eligible_count": (
                self.intervention_direction_eligible_count
            ),
            "intervention_direction_ineligible_count": (
                self.intervention_direction_ineligible_count
            ),
            "intervention_direction_agreement_count": (
                self.intervention_direction_agreement_count
            ),
            "intervention_direction_change_count": (
                self.intervention_direction_change_count
            ),
            "intervention_direction_agreement_rate": (
                self.intervention_direction_agreement_rate
            ),
            "mean_agency_delta_lead_time": self.mean_agency_delta_lead_time,
            "mean_agency_delta_throughput": self.mean_agency_delta_throughput,
            "mean_agency_delta_operating_cost": self.mean_agency_delta_operating_cost,
            "mean_adaptation_actor_time": self.mean_adaptation_actor_time,
            "mean_adaptation_cost": self.mean_adaptation_cost,
        }

    @property
    def report_hash(self) -> str:
        payload = json.dumps(
            self.canonical_payload(),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
        return sha256(payload).hexdigest()


def build_a1_scientific_report_v1(
    experiment: A0A1ReferenceExperimentResultV1,
) -> A1ScientificReportV1:
    """Compare intervention effects under fixed A0 versus adaptive A1 agency."""

    lead_metric = next(
        (
            outcome
            for outcome in experiment.design.protocol.outcomes
            if outcome.name == "lead_time"
        ),
        None,
    )
    if lead_metric is None:
        raise ValueError("A1 scientific report requires lead_time outcome")
    margin = lead_metric.equivalence_margin

    replication_plan = experiment.design.protocol.replication_plan
    if replication_plan.min_replications != replication_plan.max_replications:
        raise ValueError("A1 scientific report requires a fixed replication plan")
    expected_identities = {
        (design_index, arm_id, replication)
        for design_index in range(experiment.design.protocol.sample_size)
        for arm_id in ("baseline", *experiment.design.protocol.intervention_ids)
        for replication in range(replication_plan.min_replications)
    }
    identities = [
        (pair.design_index, pair.arm_id, pair.replication)
        for pair in experiment.pairs
    ]
    if set(identities) != expected_identities or len(identities) != len(expected_identities):
        raise ValueError("scientific report requires complete unique paired evidence")

    by_key = {
        (pair.design_index, pair.arm_id, pair.replication): pair
        for pair in experiment.pairs
    }
    intervention_effects: list[A1InterventionEffectV1] = []

    design_indices = sorted({pair.design_index for pair in experiment.pairs})
    intervention_ids = experiment.design.protocol.intervention_ids
    replications = sorted({pair.replication for pair in experiment.pairs})

    for design_index in design_indices:
        for arm_id in intervention_ids:
            a0_lead: list[float] = []
            a1_lead: list[float] = []
            a0_throughput: list[float] = []
            a1_throughput: list[float] = []
            a0_cost: list[float] = []
            a1_cost: list[float] = []
            direction_eligibility: list[bool] = []

            for replication in replications:
                baseline = by_key.get((design_index, "baseline", replication))
                intervention = by_key.get((design_index, arm_id, replication))
                if baseline is None or intervention is None:
                    raise ValueError(
                        "scientific report requires exact baseline/intervention pairing"
                    )
                a0_lead.append(
                    intervention.a0_run.mean_lead_time
                    - baseline.a0_run.mean_lead_time
                )
                a1_lead.append(
                    intervention.a1_run.mean_lead_time
                    - baseline.a1_run.mean_lead_time
                )
                a0_throughput.append(
                    intervention.a0_run.throughput - baseline.a0_run.throughput
                )
                a1_throughput.append(
                    intervention.a1_run.throughput - baseline.a1_run.throughput
                )
                a0_cost.append(
                    intervention.a0_run.total_operating_cost
                    - baseline.a0_run.total_operating_cost
                )
                a1_cost.append(
                    intervention.a1_run.total_operating_cost
                    - baseline.a1_run.total_operating_cost
                )
                direction_eligibility.append(
                    baseline.a0_stable
                    and intervention.a0_stable
                    and baseline.a1_regime is not A1RegimeKind.SATURATED
                    and intervention.a1_regime is not A1RegimeKind.SATURATED
                )

            if len(set(direction_eligibility)) != 1:
                raise ValueError("regime eligibility must be constant across replications")
            direction_eligible = direction_eligibility[0]
            a0_delta_lead = fmean(a0_lead)
            a1_delta_lead = fmean(a1_lead)
            a0_direction = _direction(a0_delta_lead, margin) if direction_eligible else None
            a1_direction = _direction(a1_delta_lead, margin) if direction_eligible else None
            intervention_effects.append(
                A1InterventionEffectV1(
                    design_index=design_index,
                    arm_id=arm_id,
                    replications=len(replications),
                    a0_delta_mean_lead_time=a0_delta_lead,
                    a1_delta_mean_lead_time=a1_delta_lead,
                    a0_delta_throughput=fmean(a0_throughput),
                    a1_delta_throughput=fmean(a1_throughput),
                    a0_delta_operating_cost=fmean(a0_cost),
                    a1_delta_operating_cost=fmean(a1_cost),
                    direction_eligible=direction_eligible,
                    a0_direction=a0_direction,
                    a1_direction=a1_direction,
                    direction_changed=(
                        a0_direction != a1_direction
                        if direction_eligible
                        else None
                    ),
                    agency_delta_intervention_effect=a1_delta_lead - a0_delta_lead,
                )
            )

    eligible_effects = tuple(
        effect for effect in intervention_effects if effect.direction_eligible
    )
    agreement = sum(
        effect.a0_direction == effect.a1_direction
        for effect in eligible_effects
    )
    changes = len(eligible_effects) - agreement

    return A1ScientificReportV1(
        protocol_hash=experiment.design.protocol.protocol_hash,
        pair_count=len(experiment.pairs),
        regime_summary=experiment.summary,
        intervention_effects=tuple(intervention_effects),
        intervention_direction_eligible_count=len(eligible_effects),
        intervention_direction_ineligible_count=(
            len(intervention_effects) - len(eligible_effects)
        ),
        intervention_direction_agreement_count=agreement,
        intervention_direction_change_count=changes,
        intervention_direction_agreement_rate=(
            agreement / len(eligible_effects)
            if eligible_effects
            else None
        ),
        mean_agency_delta_lead_time=fmean(
            pair.delta_mean_lead_time for pair in experiment.pairs
        ),
        mean_agency_delta_throughput=fmean(
            pair.delta_throughput for pair in experiment.pairs
        ),
        mean_agency_delta_operating_cost=fmean(
            pair.delta_operating_cost for pair in experiment.pairs
        ),
        mean_adaptation_actor_time=experiment.summary.mean_adaptation_actor_time,
        mean_adaptation_cost=experiment.summary.mean_adaptation_cost,
    )


def _direction(value: float, margin: float) -> int:
    if abs(value) <= margin:
        return 0
    return -1 if value < 0.0 else 1

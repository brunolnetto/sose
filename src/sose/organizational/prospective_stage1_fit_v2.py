from __future__ import annotations

from bisect import bisect_right
from hashlib import sha256
import json
from math import ceil
from statistics import fmean, median
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

from sose.core.randomness import CounterRandomSource

from .dataset import ObservedPRKey
from .model_spec import EvidenceClass, ModelSpec
from .prospective_cohort_v2 import ProspectiveCohortStatus
from .prospective_model_freeze_v2 import REQUIRED_TAIL_METRICS_V2
from .prospective_state_v2 import ProspectiveEvidenceStateV2
from .source_evidence_v2 import GitHubPREvidenceRecordV2
from .validation import LeadTimeValidationCriteria


STAGE1_FIT_VERSION_V2 = "pr-review-prospective-stage1-fit/v2"
REQUIRED_STAGE2_TAIL_METRICS_V2 = REQUIRED_TAIL_METRICS_V2

NonBlankString = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]


class DelayAnalogV2(BaseModel):
    """One training-only item-delay analog.

    Workflow-active time is directly observed machine activity. The remaining
    elapsed time is deliberately causal-subtype agnostic: it is not actor effort.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    workflow_active_seconds: float = Field(ge=0.0, allow_inf_nan=False)
    unidentified_residual_seconds: float = Field(ge=0.0, allow_inf_nan=False)

    @property
    def lead_time_seconds(self) -> float:
        return self.workflow_active_seconds + self.unidentified_residual_seconds


class PRReviewV2TailModel(BaseModel):
    """Frozen empirical analog model for prospective PR lead-time prediction."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    source_snapshot_hash: NonBlankString
    human_analogs: tuple[DelayAnalogV2, ...] = Field(min_length=1)
    bot_analogs: tuple[DelayAnalogV2, ...] = ()

    def analog_pool(self, *, author_is_bot: bool) -> tuple[DelayAnalogV2, ...]:
        if author_is_bot and self.bot_analogs:
            return self.bot_analogs
        return self.human_analogs

    def build_model_spec(self) -> ModelSpec:
        return ModelSpec(
            stations={
                "workflow_activity": {
                    "kind": "observed-machine-delay-distribution",
                    "accounting": "union-active-time",
                },
                "unidentified_elapsed_delay": {
                    "kind": "item-delay-distribution",
                    "causal_subtype": "unidentified",
                },
            },
            routing={
                "item_flow": [
                    "workflow_activity",
                    "unidentified_elapsed_delay",
                    "complete",
                ]
            },
            policies={
                "analog_sampling": "paired-empirical-with-replacement",
                "stratification": "author_is_bot_at_item_creation",
                "bot_pool_fallback": "human_pool_if_training_has_no_bot_items",
            },
            parameters={
                "human_delay_analog_pairs": [
                    [
                        analog.workflow_active_seconds,
                        analog.unidentified_residual_seconds,
                    ]
                    for analog in self.human_analogs
                ],
                "bot_delay_analog_pairs": [
                    [
                        analog.workflow_active_seconds,
                        analog.unidentified_residual_seconds,
                    ]
                    for analog in self.bot_analogs
                ],
                "bot_stratification": True,
            },
            parameter_evidence={
                "human_delay_analog_pairs": EvidenceClass.INFERABLE,
                "bot_delay_analog_pairs": EvidenceClass.INFERABLE,
                "bot_stratification": EvidenceClass.OBSERVED,
            },
        )


class PRReviewV2Case(BaseModel):
    """Prediction-time inputs available when the pull request is created."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    pr_id: NonBlankString
    opened_at: float = Field(ge=0.0, allow_inf_nan=False)
    author_is_bot: bool


class PRReviewV2Prediction(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    lead_time_seconds: dict[str, float]
    completed_at: dict[str, float]


class Stage1PRReviewFitV2(BaseModel):
    """Hash-addressed Stage-1-only fit and Stage-2 acceptance contract."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    fit_version: str = STAGE1_FIT_VERSION_V2
    training_state_hash: NonBlankString
    protocol_hash: NonBlankString
    snapshot_hash: NonBlankString
    training_keys: tuple[ObservedPRKey, ...]
    model: PRReviewV2TailModel
    model_spec: ModelSpec
    model_spec_hash: NonBlankString
    simulation_seed: int = Field(ge=0)
    training_bot_fraction: float = Field(ge=0.0, le=1.0, allow_inf_nan=False)
    acceptance_bootstrap_seed: int = Field(ge=0)
    acceptance_bootstrap_replicates: int = Field(ge=32)
    acceptance_quantile: float = Field(gt=0.5, lt=1.0, allow_inf_nan=False)
    acceptance_criteria: LeadTimeValidationCriteria
    acceptance_criteria_hash: NonBlankString
    tail_metrics: tuple[str, ...]
    source_normalization_rules: NonBlankString
    missing_data_policy: NonBlankString
    fitting_rule: NonBlankString

    @model_validator(mode="after")
    def validate_derivations(self) -> "Stage1PRReviewFitV2":
        if self.model_spec_hash != self.model_spec.model_spec_hash:
            raise ValueError("model_spec_hash must match fitted ModelSpec")
        if self.acceptance_criteria_hash != self.acceptance_criteria.criteria_hash:
            raise ValueError("acceptance_criteria_hash must match acceptance criteria")
        if self.tail_metrics != REQUIRED_STAGE2_TAIL_METRICS_V2:
            raise ValueError("tail metrics must exactly match prospective v2 requirements")
        return self

    def canonical_payload(self) -> dict[str, object]:
        return {
            "fit_version": self.fit_version,
            "training_state_hash": self.training_state_hash,
            "protocol_hash": self.protocol_hash,
            "snapshot_hash": self.snapshot_hash,
            "training_keys": [list(key) for key in self.training_keys],
            "model": self.model.model_dump(mode="json"),
            "model_spec": self.model_spec.canonical_payload(),
            "model_spec_hash": self.model_spec_hash,
            "simulation_seed": self.simulation_seed,
            "training_bot_fraction": self.training_bot_fraction,
            "acceptance_bootstrap_seed": self.acceptance_bootstrap_seed,
            "acceptance_bootstrap_replicates": self.acceptance_bootstrap_replicates,
            "acceptance_quantile": self.acceptance_quantile,
            "acceptance_criteria": self.acceptance_criteria.model_dump(mode="json"),
            "acceptance_criteria_hash": self.acceptance_criteria_hash,
            "tail_metrics": list(self.tail_metrics),
            "source_normalization_rules": self.source_normalization_rules,
            "missing_data_policy": self.missing_data_policy,
            "fitting_rule": self.fitting_rule,
        }

    def canonical_json(self) -> str:
        return json.dumps(
            self.canonical_payload(),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        )

    @property
    def fit_hash(self) -> str:
        return sha256(self.canonical_json().encode("utf-8")).hexdigest()


def workflow_active_seconds_v2(record: GitHubPREvidenceRecordV2) -> float:
    """Union machine-active intervals inside the item open→merge lifetime."""

    intervals: list[tuple[object, object]] = []
    for job in record.workflow_jobs:
        started = max(job.started_at, record.opened_at)
        completed = min(job.completed_at, record.merged_at)
        if completed > started:
            intervals.append((started, completed))
    intervals.sort(key=lambda interval: interval[0])

    merged: list[list[object]] = []
    for started, completed in intervals:
        if not merged or started > merged[-1][1]:
            merged.append([started, completed])
            continue
        if completed > merged[-1][1]:
            merged[-1][1] = completed

    return sum(
        (completed - started).total_seconds()
        for started, completed in merged
    )


def fit_stage1_pr_review_v2(
    *,
    state: ProspectiveEvidenceStateV2,
    simulation_seed: int,
    acceptance_bootstrap_seed: int,
    acceptance_bootstrap_replicates: int = 2_048,
    acceptance_quantile: float = 0.95,
) -> Stage1PRReviewFitV2:
    """Fit only the persisted Stage-1 training partition; ignore interstitial items."""

    if state.cohort.status is not ProspectiveCohortStatus.AWAITING_MODEL_FREEZE:
        raise ValueError("Stage-1 model fitting requires a complete pre-freeze training cohort")
    if len(state.cohort.training_keys) != state.cohort.training_count:
        raise ValueError("Stage-1 training cohort is incomplete")
    if state.cohort.holdout_keys or state.cohort.post_holdout_keys:
        raise ValueError("Stage-1 fitting cannot consume holdout evidence")

    by_key = {
        (record.repository, record.pr_number): record
        for record in state.snapshot.records
    }
    training_records = tuple(by_key[key] for key in state.cohort.training_keys)
    analogs = tuple(_analog(record) for record in training_records)
    human_analogs = tuple(
        analog
        for record, analog in zip(training_records, analogs, strict=True)
        if not record.author_is_bot
    )
    bot_analogs = tuple(
        analog
        for record, analog in zip(training_records, analogs, strict=True)
        if record.author_is_bot
    )
    if not human_analogs:
        raise ValueError("prospective v2 requires at least one non-bot training item")

    model = PRReviewV2TailModel(
        source_snapshot_hash=state.snapshot_hash,
        human_analogs=human_analogs,
        bot_analogs=bot_analogs,
    )
    model_spec = model.build_model_spec()
    bot_fraction = len(bot_analogs) / len(training_records)
    criteria = derive_stage2_acceptance_criteria_v2(
        model=model,
        training_bot_fraction=bot_fraction,
        holdout_count=state.cohort.holdout_count,
        seed=acceptance_bootstrap_seed,
        replicates=acceptance_bootstrap_replicates,
        quantile=acceptance_quantile,
    )
    return Stage1PRReviewFitV2(
        training_state_hash=state.state_hash,
        protocol_hash=state.protocol_hash,
        snapshot_hash=state.snapshot_hash,
        training_keys=state.cohort.training_keys,
        model=model,
        model_spec=model_spec.canonical_payload(),
        model_spec_hash=model_spec.model_spec_hash,
        simulation_seed=simulation_seed,
        training_bot_fraction=bot_fraction,
        acceptance_bootstrap_seed=acceptance_bootstrap_seed,
        acceptance_bootstrap_replicates=acceptance_bootstrap_replicates,
        acceptance_quantile=acceptance_quantile,
        acceptance_criteria=criteria,
        acceptance_criteria_hash=criteria.criteria_hash,
        tail_metrics=REQUIRED_STAGE2_TAIL_METRICS_V2,
        source_normalization_rules=(
            "For each training PR, clip every observed workflow-job interval to "
            "[opened_at, merged_at], union overlapping intervals without double "
            "counting, and define workflow_active_seconds from that union. "
            "author_is_bot is read only from item-creation source identity."
        ),
        missing_data_policy=(
            "A training item with no workflow intervals contributes zero observed "
            "workflow-active seconds. Missing human effort, calendars, meetings, "
            "interruptions, or causal blocking subtype are never reconstructed from "
            "timestamp gaps."
        ),
        fitting_rule=(
            "Retain every preregistered Stage-1 item and every tail observation. "
            "Fit paired empirical analogs (workflow-active seconds, unidentified "
            "residual elapsed seconds), stratified only by author_is_bot. Stage-2 "
            "prediction samples one paired analog with counter-keyed RNG; no actor "
            "service time or capacity is inferred."
        ),
    )


def predict_pr_review_v2(
    *,
    cases: tuple[PRReviewV2Case, ...],
    model: PRReviewV2TailModel,
    seed: int,
) -> PRReviewV2Prediction:
    if not cases:
        raise ValueError("at least one prediction case is required")
    ids = tuple(case.pr_id for case in cases)
    if len(ids) != len(set(ids)):
        raise ValueError("prediction case ids must be unique")

    rng = CounterRandomSource(seed)
    lead_times: dict[str, float] = {}
    completed_at: dict[str, float] = {}
    for case in cases:
        analog = _sample_analog(
            model=model,
            author_is_bot=case.author_is_bot,
            rng=rng,
            entity_id=case.pr_id,
            mechanism="prospective_v2_delay_analog",
        )
        lead = analog.lead_time_seconds
        lead_times[case.pr_id] = lead
        completed_at[case.pr_id] = case.opened_at + lead
    return PRReviewV2Prediction(
        lead_time_seconds=lead_times,
        completed_at=completed_at,
    )


def derive_stage2_acceptance_criteria_v2(
    *,
    model: PRReviewV2TailModel,
    training_bot_fraction: float,
    holdout_count: int,
    seed: int,
    replicates: int,
    quantile: float,
) -> LeadTimeValidationCriteria:
    """Derive separate Stage-2 discrepancy limits from training-only bootstrap variation."""

    if not 0.0 <= training_bot_fraction <= 1.0:
        raise ValueError("training_bot_fraction must be between zero and one")
    if holdout_count < 2:
        raise ValueError("holdout_count must be at least two")
    if replicates < 32:
        raise ValueError("replicates must be at least 32")
    if not 0.5 < quantile < 1.0:
        raise ValueError("quantile must be between 0.5 and 1.0")

    rng = CounterRandomSource(seed)
    mean_errors: list[float] = []
    median_errors: list[float] = []
    p90_errors: list[float] = []
    ecdf_distances: list[float] = []

    for replicate in range(replicates):
        observed: list[float] = []
        predicted: list[float] = []
        for index in range(holdout_count):
            entity = f"bootstrap:{replicate}:{index}"
            is_bot = rng.bernoulli(
                training_bot_fraction,
                stream="stage2_acceptance",
                entity_id=entity,
                mechanism="author_is_bot",
            )
            observed.append(
                _sample_analog(
                    model=model,
                    author_is_bot=is_bot,
                    rng=rng,
                    entity_id=entity,
                    mechanism="observed_reference",
                ).lead_time_seconds
            )
            predicted.append(
                _sample_analog(
                    model=model,
                    author_is_bot=is_bot,
                    rng=rng,
                    entity_id=entity,
                    mechanism="predicted_reference",
                ).lead_time_seconds
            )

        mean_errors.append(abs(fmean(predicted) - fmean(observed)))
        median_errors.append(abs(median(predicted) - median(observed)))
        p90_errors.append(abs(_p90(predicted) - _p90(observed)))
        ecdf_distances.append(_ecdf_distance(observed, predicted))

    return LeadTimeValidationCriteria(
        max_abs_mean_difference_seconds=_nearest_rank(mean_errors, quantile),
        max_abs_median_difference_seconds=_nearest_rank(median_errors, quantile),
        max_abs_p90_difference_seconds=_nearest_rank(p90_errors, quantile),
        max_ecdf_distance=_nearest_rank(ecdf_distances, quantile),
    )


def _analog(record: GitHubPREvidenceRecordV2) -> DelayAnalogV2:
    lead_time = (record.merged_at - record.opened_at).total_seconds()
    workflow_active = workflow_active_seconds_v2(record)
    residual = lead_time - workflow_active
    if residual < -1e-9:
        raise ValueError("workflow-active union cannot exceed item lead time")
    return DelayAnalogV2(
        workflow_active_seconds=workflow_active,
        unidentified_residual_seconds=max(0.0, residual),
    )


def _sample_analog(
    *,
    model: PRReviewV2TailModel,
    author_is_bot: bool,
    rng: CounterRandomSource,
    entity_id: str,
    mechanism: str,
) -> DelayAnalogV2:
    pool = model.analog_pool(author_is_bot=author_is_bot)
    uniform = rng.uniform(
        stream="prospective_v2",
        entity_id=entity_id,
        mechanism=mechanism,
    )
    index = min(int(uniform * len(pool)), len(pool) - 1)
    return pool[index]


def _p90(values: list[float]) -> float:
    ordered = sorted(values)
    return ordered[max(1, ceil(0.90 * len(ordered))) - 1]


def _nearest_rank(values: list[float], quantile: float) -> float:
    ordered = sorted(values)
    return ordered[max(1, ceil(quantile * len(ordered))) - 1]


def _ecdf_distance(left: list[float], right: list[float]) -> float:
    left_sorted = sorted(left)
    right_sorted = sorted(right)
    support = sorted(set(left_sorted) | set(right_sorted))
    return max(
        abs(
            bisect_right(left_sorted, value) / len(left_sorted)
            - bisect_right(right_sorted, value) / len(right_sorted)
        )
        for value in support
    )

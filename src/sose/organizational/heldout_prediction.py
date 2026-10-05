from __future__ import annotations

from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from sose.examples.organizational_pr_review import (
    PullRequestCase,
    PullRequestFlowConfig,
    PullRequestFlowEvidence,
    simulate_pull_request_flow,
)

from .calibration import ObservedItemFlowCalibration, calibrate_observed_item_flow
from .dataset import ObservedPRDataset, ObservedPRSplit
from .model_spec import EvidenceClass
from .validation import LeadTimeValidation, compare_lead_time_distributions


NonBlankString = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]


class PRReviewAssumptions(BaseModel):
    """Actor-side and otherwise unidentified parameters supplied explicitly by the experiment."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    reviewer_count: int = Field(ge=1)
    mean_review_time_seconds: float = Field(gt=0.0, allow_inf_nan=False)
    mean_revision_time_seconds: float = Field(gt=0.0, allow_inf_nan=False)
    rework_probability: float = Field(ge=0.0, lt=1.0, allow_inf_nan=False)
    fallback_ci_time_seconds: float = Field(gt=0.0, allow_inf_nan=False)


class PRReviewHeldoutPrediction(BaseModel):
    """One deterministic train→predict→held-out validation result."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    train_dataset_hash: NonBlankString
    holdout_dataset_hash: NonBlankString
    calibration: ObservedItemFlowCalibration
    config: PullRequestFlowConfig
    evidence: PullRequestFlowEvidence
    model_spec_hash: NonBlankString
    holdout_case_opened_seconds: tuple[float, ...]
    simulated_lead_times_seconds: tuple[float, ...]
    validation: LeadTimeValidation


def predict_pr_review_holdout(
    *,
    split: ObservedPRSplit,
    assumptions: PRReviewAssumptions,
    seed: int,
) -> PRReviewHeldoutPrediction:
    """Calibrate on train only, then predict held-out lead times without using held-out outcomes."""

    _require_terminal(split.train, role="training")
    _require_terminal(split.holdout, role="holdout")

    calibration = calibrate_observed_item_flow(split.train)
    ci_summary = calibration.ci_duration_seconds
    use_observed_ci = ci_summary is not None and ci_summary.mean > 0.0
    ci_time = ci_summary.mean if use_observed_ci else assumptions.fallback_ci_time_seconds

    config = PullRequestFlowConfig(
        reviewer_count=assumptions.reviewer_count,
        ci_time=ci_time,
        mean_review_time=assumptions.mean_review_time_seconds,
        mean_revision_time=assumptions.mean_revision_time_seconds,
        rework_probability=assumptions.rework_probability,
    )
    evidence = PullRequestFlowEvidence(
        reviewer_count=EvidenceClass.ASSUMED,
        ci_time=EvidenceClass.OBSERVED if use_observed_ci else EvidenceClass.ASSUMED,
        mean_review_time=EvidenceClass.ASSUMED,
        mean_revision_time=EvidenceClass.ASSUMED,
        rework_probability=EvidenceClass.ASSUMED,
    )

    holdout_start = min(trace.opened_at for trace in split.holdout.traces)
    cases = tuple(
        PullRequestCase(
            pr_id=f"{trace.repository}#{trace.pr_number}",
            opened_at=(trace.opened_at - holdout_start).total_seconds(),
        )
        for trace in split.holdout.traces
    )
    simulation = simulate_pull_request_flow(
        cases=cases,
        config=config,
        seed=seed,
        evidence=evidence,
    )
    simulated_lead_times = tuple(
        simulation.completed_at[case.pr_id] - case.opened_at
        for case in cases
    )
    validation = compare_lead_time_distributions(
        observed=split.holdout,
        simulated_seconds=simulated_lead_times,
    )

    return PRReviewHeldoutPrediction(
        train_dataset_hash=split.train.dataset_hash,
        holdout_dataset_hash=split.holdout.dataset_hash,
        calibration=calibration,
        config=config,
        evidence=evidence,
        model_spec_hash=simulation.model_spec_hash,
        holdout_case_opened_seconds=tuple(case.opened_at for case in cases),
        simulated_lead_times_seconds=simulated_lead_times,
        validation=validation,
    )


def _require_terminal(dataset: ObservedPRDataset, *, role: str) -> None:
    if any(trace.terminal_at is None for trace in dataset.traces):
        raise ValueError(f"held-out prediction requires a terminal-only {role} dataset")

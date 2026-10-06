from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from .empirical_pilot import GitHubPRObservationSnapshot
from .preregistration import PRReviewPreregisteredPilotResult


class PRReviewValidationDiagnostic(BaseModel):
    """Descriptive failure analysis that cannot alter the frozen acceptance result."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    source_snapshot_hash: str = Field(min_length=1)
    execution_hash: str = Field(min_length=1)
    acceptance_unchanged: Literal[True] = True
    original_gate_passed: bool
    failing_metrics: tuple[str, ...]
    largest_observed_key: tuple[str, int]
    largest_observed_lead_time_seconds: float = Field(ge=0.0, allow_inf_nan=False)
    observed_max_share_of_total_lead_time: float = Field(ge=0.0, le=1.0, allow_inf_nan=False)
    observed_max_to_median_ratio: float = Field(ge=0.0, allow_inf_nan=False)
    simulated_max_to_median_ratio: float = Field(ge=0.0, allow_inf_nan=False)
    max_tail_gap_seconds: float = Field(allow_inf_nan=False)


def diagnose_pr_review_validation(
    *,
    snapshot: GitHubPRObservationSnapshot,
    execution: PRReviewPreregisteredPilotResult,
) -> PRReviewValidationDiagnostic:
    """Describe a frozen held-out result without excluding items or recomputing its gate."""

    if snapshot.snapshot_hash != execution.result.snapshot_hash:
        raise ValueError("snapshot hash does not match frozen validation execution")

    records = {(record.repository, record.pr_number): record for record in snapshot.records}
    observed: list[tuple[tuple[str, int], float]] = []
    for key in execution.result.holdout_keys:
        record = records.get(key)
        if record is None:
            raise ValueError(f"holdout item missing from source snapshot: {key!r}")
        observed.append((key, (record.merged_at - record.opened_at).total_seconds()))

    if not observed:
        raise ValueError("validation diagnostic requires a non-empty holdout")

    simulated = execution.result.prediction.simulated_lead_times_seconds
    if not simulated:
        raise ValueError("validation diagnostic requires simulated holdout lead times")

    largest_key, observed_max = max(observed, key=lambda entry: (entry[1], entry[0]))
    observed_total = sum(value for _, value in observed)
    observed_median = execution.result.prediction.validation.observed.median
    simulated_median = execution.result.prediction.validation.simulated.median
    simulated_max = max(simulated)
    assessment = execution.result.validation_assessment

    return PRReviewValidationDiagnostic(
        source_snapshot_hash=snapshot.snapshot_hash,
        execution_hash=execution.execution_hash,
        original_gate_passed=assessment.passed,
        failing_metrics=tuple(check.metric for check in assessment.checks if not check.passed),
        largest_observed_key=largest_key,
        largest_observed_lead_time_seconds=observed_max,
        observed_max_share_of_total_lead_time=(
            observed_max / observed_total if observed_total > 0.0 else 0.0
        ),
        observed_max_to_median_ratio=(
            observed_max / observed_median if observed_median > 0.0 else 0.0
        ),
        simulated_max_to_median_ratio=(
            simulated_max / simulated_median if simulated_median > 0.0 else 0.0
        ),
        max_tail_gap_seconds=observed_max - simulated_max,
    )

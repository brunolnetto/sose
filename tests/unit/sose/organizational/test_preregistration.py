from __future__ import annotations

from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from sose.organizational.empirical_pilot import GitHubPRObservationSnapshot, GitHubPRSourceRecord
from sose.organizational.heldout_prediction import PRReviewAssumptions
from sose.organizational.preregistration import (
    PRReviewEmpiricalPlan,
    PRReviewPreregisteredPilotResult,
    execute_pr_review_empirical_plan,
)
from sose.organizational.validation import LeadTimeValidationCriteria

UTC = timezone.utc
REPOSITORY = "brunolnetto/sose"


def _dt(hour: int, minute: int, second: int = 0) -> datetime:
    return datetime(2026, 10, 5, hour, minute, second, tzinfo=UTC)


def _record(number: int, opened: datetime, merged: datetime) -> GitHubPRSourceRecord:
    return GitHubPRSourceRecord(repository=REPOSITORY, pr_number=number, opened_at=opened, merged_at=merged, source_url=f"https://api.github.com/repos/brunolnetto/sose/pulls/{number}")


def _snapshot() -> GitHubPRObservationSnapshot:
    return GitHubPRObservationSnapshot(
        snapshot_version="sose-prs-265-274@2026-10-05",
        records=(
            _record(265, _dt(14, 18, 5), _dt(14, 18, 49)),
            _record(266, _dt(14, 22, 22), _dt(14, 26, 6)),
            _record(267, _dt(14, 28, 20), _dt(14, 45, 25)),
            _record(268, _dt(14, 40, 52), _dt(15, 20, 17)),
            _record(269, _dt(14, 48, 34), _dt(15, 32, 14)),
            _record(270, _dt(15, 34, 51), _dt(15, 49, 11)),
            _record(271, _dt(15, 46, 54), _dt(16, 50, 55)),
            _record(272, _dt(15, 56, 43), _dt(16, 56, 40)),
            _record(273, _dt(17, 3, 34), _dt(17, 7, 32)),
            _record(274, _dt(17, 13, 19), _dt(17, 27, 57)),
        ),
    )


def _assumptions() -> PRReviewAssumptions:
    return PRReviewAssumptions(reviewer_count=1, mean_review_time_seconds=300.0, mean_revision_time_seconds=180.0, rework_probability=0.15, fallback_ci_time_seconds=120.0)


def _criteria(limit_seconds: float = 3600.0) -> LeadTimeValidationCriteria:
    return LeadTimeValidationCriteria(max_abs_mean_difference_seconds=limit_seconds, max_abs_median_difference_seconds=limit_seconds, max_abs_p90_difference_seconds=limit_seconds, max_ecdf_distance=0.5)


def _plan(snapshot: GitHubPRObservationSnapshot, *, seed: int = 20261005) -> PRReviewEmpiricalPlan:
    return PRReviewEmpiricalPlan(snapshot_hash=snapshot.snapshot_hash, split_policy="chronological-purged/v1", holdout_fraction=0.30, assumptions=_assumptions(), validation_criteria=_criteria(), seed=seed)


def test_preregistration_plan_is_canonical_and_hash_addressed() -> None:
    snapshot = _snapshot()
    first = _plan(snapshot)
    second = _plan(snapshot)
    assert first.plan_version == "pr-review-empirical-plan/v1"
    assert first.split_policy == "chronological-purged/v1"
    assert first.canonical_json() == second.canonical_json()
    assert first.plan_hash == second.plan_hash
    assert len(first.plan_hash) == 64


def test_preregistration_plan_identity_changes_with_declared_inputs() -> None:
    snapshot = _snapshot()
    baseline = _plan(snapshot, seed=20261005)
    changed_seed = _plan(snapshot, seed=20261006)
    changed_criteria = baseline.model_copy(update={"validation_criteria": _criteria(limit_seconds=60.0)})
    assert baseline.plan_hash != changed_seed.plan_hash
    assert baseline.plan_hash != changed_criteria.plan_hash


def test_plan_rejects_unknown_split_policy() -> None:
    snapshot = _snapshot()
    with pytest.raises(ValidationError):
        PRReviewEmpiricalPlan(snapshot_hash=snapshot.snapshot_hash, split_policy="random/v1", holdout_fraction=0.30, assumptions=_assumptions(), validation_criteria=_criteria(), seed=20261005)


def test_execute_plan_binds_plan_to_empirical_artifact() -> None:
    snapshot = _snapshot()
    plan = _plan(snapshot)
    execution = execute_pr_review_empirical_plan(snapshot=snapshot, plan=plan)
    assert execution.execution_version == "pr-review-preregistered-execution/v1"
    assert execution.plan_hash == plan.plan_hash
    assert execution.result.snapshot_hash == plan.snapshot_hash
    assert execution.result.holdout_fraction == plan.holdout_fraction
    assert execution.result.assumptions == plan.assumptions
    assert execution.result.validation_criteria == plan.validation_criteria
    assert execution.result.seed == plan.seed
    assert len(execution.execution_hash) == 64


def test_execute_plan_rejects_source_snapshot_mismatch() -> None:
    snapshot = _snapshot()
    plan = _plan(snapshot).model_copy(update={"snapshot_hash": "different-snapshot"})
    with pytest.raises(ValueError, match="snapshot hash"):
        execute_pr_review_empirical_plan(snapshot=snapshot, plan=plan)


def test_persisted_execution_rejects_result_not_matching_plan() -> None:
    snapshot = _snapshot()
    plan = _plan(snapshot)
    execution = execute_pr_review_empirical_plan(snapshot=snapshot, plan=plan)
    forged_result = execution.result.model_copy(update={"seed": plan.seed + 1})
    payload = execution.model_dump()
    payload["result"] = forged_result.model_dump()
    with pytest.raises(ValidationError, match="seed"):
        PRReviewPreregisteredPilotResult.model_validate(payload)


def test_failed_validation_is_still_a_preregistered_execution_artifact() -> None:
    snapshot = _snapshot()
    strict_plan = _plan(snapshot).model_copy(update={"validation_criteria": LeadTimeValidationCriteria(max_abs_mean_difference_seconds=0.0, max_abs_median_difference_seconds=0.0, max_abs_p90_difference_seconds=0.0, max_ecdf_distance=0.0)})
    execution = execute_pr_review_empirical_plan(snapshot=snapshot, plan=strict_plan)
    assert not execution.result.validation_assessment.passed
    assert execution.plan_hash == strict_plan.plan_hash
    assert len(execution.execution_hash) == 64

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from sose.organizational.empirical_pilot import (
    GitHubPRObservationSnapshot,
    GitHubPRSourceRecord,
    run_pr_review_empirical_pilot,
)
from sose.organizational.heldout_prediction import PRReviewAssumptions
from sose.organizational.model_spec import EvidenceClass
from sose.organizational.validation import LeadTimeValidationCriteria


UTC = timezone.utc
REPOSITORY = "brunolnetto/sose"


def _dt(hour: int, minute: int, second: int = 0) -> datetime:
    return datetime(2026, 10, 5, hour, minute, second, tzinfo=UTC)


def _record(number: int, opened: datetime, merged: datetime) -> GitHubPRSourceRecord:
    return GitHubPRSourceRecord(
        repository=REPOSITORY,
        pr_number=number,
        opened_at=opened,
        merged_at=merged,
        source_url=f"https://api.github.com/repos/brunolnetto/sose/pulls/{number}",
    )


def _snapshot() -> GitHubPRObservationSnapshot:
    # Real merged PR lifecycle timestamps from the SOSE repository. These records
    # intentionally contain item-level evidence only; they do not manufacture
    # reviewer effort, calendars, meetings, or context-switch time.
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
    return PRReviewAssumptions(
        reviewer_count=1,
        mean_review_time_seconds=300.0,
        mean_revision_time_seconds=180.0,
        rework_probability=0.15,
        fallback_ci_time_seconds=120.0,
    )


def _criteria(*, limit_seconds: float = 100_000.0, max_ecdf: float = 1.0) -> LeadTimeValidationCriteria:
    return LeadTimeValidationCriteria(
        max_abs_mean_difference_seconds=limit_seconds,
        max_abs_median_difference_seconds=limit_seconds,
        max_abs_p90_difference_seconds=limit_seconds,
        max_ecdf_distance=max_ecdf,
    )


def test_snapshot_is_hash_addressed_and_canonical() -> None:
    snapshot = _snapshot()
    reversed_snapshot = GitHubPRObservationSnapshot(
        snapshot_version=snapshot.snapshot_version,
        records=tuple(reversed(snapshot.records)),
    )

    assert snapshot.snapshot_hash == reversed_snapshot.snapshot_hash
    assert snapshot.records[0].pr_number == 265
    assert snapshot.records[-1].pr_number == 274
    assert len(snapshot.snapshot_hash) == 64


def test_snapshot_builds_terminal_observed_dataset_without_actor_inference() -> None:
    snapshot = _snapshot()
    dataset = snapshot.to_dataset()

    assert dataset.keys == tuple((REPOSITORY, number) for number in range(265, 275))
    assert all(trace.terminal_at is not None for trace in dataset.traces)
    assert all(len(trace.events) == 2 for trace in dataset.traces)


def test_real_pr_pilot_uses_purged_temporal_holdout_and_binds_provenance() -> None:
    snapshot = _snapshot()
    criteria = _criteria()
    assumptions = _assumptions()
    result = run_pr_review_empirical_pilot(
        snapshot=snapshot,
        holdout_fraction=0.30,
        assumptions=assumptions,
        validation_criteria=criteria,
        seed=20261005,
    )

    assert result.snapshot_hash == snapshot.snapshot_hash
    assert result.holdout_fraction == 0.30
    assert result.assumptions == assumptions
    assert result.seed == 20261005
    assert result.train_keys == tuple((REPOSITORY, number) for number in range(265, 271))
    assert result.purged_keys == ((REPOSITORY, 271),)
    assert result.holdout_keys == (
        (REPOSITORY, 272),
        (REPOSITORY, 273),
        (REPOSITORY, 274),
    )
    assert result.prediction.train_dataset_hash == result.train_dataset_hash
    assert result.prediction.holdout_dataset_hash == result.holdout_dataset_hash
    assert len(result.prediction.simulated_lead_times_seconds) == 3
    assert result.prediction.evidence.ci_time is EvidenceClass.ASSUMED
    assert result.prediction.evidence.mean_review_time is EvidenceClass.ASSUMED
    assert result.prediction.evidence.rework_probability is EvidenceClass.ASSUMED
    assert result.validation_criteria == criteria
    assert result.validation_assessment.criteria_hash == criteria.criteria_hash
    assert result.validation_assessment.observed_dataset_hash == result.holdout_dataset_hash
    assert result.validation_assessment.passed
    assert len(result.artifact_hash) == 64


def test_strict_preregistered_criteria_can_refute_pilot_without_hiding_prediction() -> None:
    criteria = _criteria(limit_seconds=0.0, max_ecdf=0.0)
    result = run_pr_review_empirical_pilot(
        snapshot=_snapshot(),
        holdout_fraction=0.30,
        assumptions=_assumptions(),
        validation_criteria=criteria,
        seed=20261005,
    )

    assert not result.validation_assessment.passed
    assert result.validation_assessment.criteria_hash == criteria.criteria_hash
    assert len(result.prediction.simulated_lead_times_seconds) == 3
    assert any(not check.passed for check in result.validation_assessment.checks)


def test_empirical_pilot_is_deterministic_for_same_source_assumptions_criteria_and_seed() -> None:
    kwargs = {
        "snapshot": _snapshot(),
        "holdout_fraction": 0.30,
        "assumptions": _assumptions(),
        "validation_criteria": _criteria(),
        "seed": 20261005,
    }

    first = run_pr_review_empirical_pilot(**kwargs)
    second = run_pr_review_empirical_pilot(**kwargs)

    assert first == second
    assert first.canonical_json() == second.canonical_json()
    assert first.artifact_hash == second.artifact_hash


def test_empirical_artifact_identity_binds_execution_inputs() -> None:
    common = {
        "snapshot": _snapshot(),
        "holdout_fraction": 0.30,
        "assumptions": _assumptions(),
        "validation_criteria": _criteria(),
    }
    first = run_pr_review_empirical_pilot(**common, seed=20261005)
    second = run_pr_review_empirical_pilot(**common, seed=20261006)

    assert first.seed != second.seed
    assert first.artifact_hash != second.artifact_hash
    assert first.canonical_payload()["seed"] == 20261005
    assert first.canonical_payload()["holdout_fraction"] == 0.30
    assert first.canonical_payload()["assumptions"] == _assumptions().model_dump(mode="json")


def test_snapshot_rejects_duplicate_prs_and_non_terminal_or_naive_records() -> None:
    record = _record(265, _dt(14, 18, 5), _dt(14, 18, 49))
    with pytest.raises(ValidationError, match="duplicate"):
        GitHubPRObservationSnapshot(snapshot_version="duplicate", records=(record, record))

    with pytest.raises(ValidationError, match="timezone-aware"):
        GitHubPRSourceRecord(
            repository=REPOSITORY,
            pr_number=1,
            opened_at=datetime(2026, 10, 5, 12, 0),
            merged_at=_dt(12, 5),
            source_url="https://api.github.com/repos/brunolnetto/sose/pulls/1",
        )

    with pytest.raises(ValidationError, match="after opened_at"):
        GitHubPRSourceRecord(
            repository=REPOSITORY,
            pr_number=2,
            opened_at=_dt(12, 5),
            merged_at=_dt(12, 0),
            source_url="https://api.github.com/repos/brunolnetto/sose/pulls/2",
        )

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from sose.organizational.calibration import calibrate_observed_item_flow
from sose.organizational.empirical_pilot import (
    EmpiricalEligibilityCriteria,
    GitHubPRObservationSnapshot,
    GitHubPRSourceRecord,
    GitHubReviewRequestAction,
    GitHubReviewRequestSourceRecord,
    GitHubReviewSubmissionSourceRecord,
    assess_snapshot_evidence,
    evaluate_empirical_eligibility,
)


UTC = timezone.utc
REPOSITORY = "brunolnetto/sose"


def _dt(day: int, hour: int, minute: int, second: int = 0) -> datetime:
    return datetime(2026, 10, day, hour, minute, second, tzinfo=UTC)


def _real_pr_258() -> GitHubPRSourceRecord:
    return GitHubPRSourceRecord(
        repository=REPOSITORY,
        pr_number=258,
        opened_at=_dt(2, 11, 49, 32),
        merged_at=_dt(3, 10, 59, 35),
        source_url="https://api.github.com/repos/brunolnetto/sose/pulls/258",
        review_request_events=(
            GitHubReviewRequestSourceRecord(
                event_id=32331005944,
                action=GitHubReviewRequestAction.REQUESTED,
                reviewer_key="Copilot",
                occurred_at=_dt(2, 11, 49, 33),
                source_url="https://api.github.com/repos/brunolnetto/sose/issues/events/32331005944",
            ),
        ),
        review_submissions=(
            GitHubReviewSubmissionSourceRecord(
                review_id=5391417989,
                reviewer_key="Copilot",
                submitted_at=_dt(2, 11, 51, 27),
                state="commented",
                source_url="https://github.com/brunolnetto/sose/pull/258#pullrequestreview-5391417989",
            ),
        ),
    )


def _no_review_pr() -> GitHubPRSourceRecord:
    return GitHubPRSourceRecord(
        repository=REPOSITORY,
        pr_number=265,
        opened_at=_dt(5, 14, 18, 5),
        merged_at=_dt(5, 14, 18, 49),
        source_url="https://api.github.com/repos/brunolnetto/sose/pulls/265",
    )


def test_real_pr_review_request_and_submission_produce_observed_response_latency_only() -> None:
    snapshot = GitHubPRObservationSnapshot(
        snapshot_version="sose-review-control@2026-10-05",
        records=(_real_pr_258(), _no_review_pr()),
    )

    dataset = snapshot.to_dataset()
    trace = next(trace for trace in dataset.traces if trace.pr_number == 258)
    assert trace.review_response_latencies_seconds() == (114.0,)

    calibration = calibrate_observed_item_flow(dataset)
    assert calibration.review_response_latency_seconds is not None
    assert calibration.review_response_latency_seconds.count == 1
    assert calibration.review_response_latency_seconds.mean == pytest.approx(114.0)
    assert "reviewer_service_time" in calibration.unidentified_actor_parameters
    assert not hasattr(calibration, "reviewer_service_time")

    evidence = assess_snapshot_evidence(snapshot)
    assert evidence.review_request_event_count == 1
    assert evidence.review_submission_count == 1
    assert evidence.paired_review_response_count == 1
    assert evidence.review_response_pr_count == 1
    assert evidence.review_response_observed is True


def test_removed_request_is_not_paired_with_later_submission() -> None:
    record = GitHubPRSourceRecord(
        repository=REPOSITORY,
        pr_number=1,
        opened_at=_dt(5, 9, 0),
        merged_at=_dt(5, 12, 0),
        source_url="https://api.github.com/example/pulls/1",
        review_request_events=(
            GitHubReviewRequestSourceRecord(
                event_id=1,
                action=GitHubReviewRequestAction.REQUESTED,
                reviewer_key="reviewer",
                occurred_at=_dt(5, 9, 5),
                source_url="https://api.github.com/example/events/1",
            ),
            GitHubReviewRequestSourceRecord(
                event_id=2,
                action=GitHubReviewRequestAction.REMOVED,
                reviewer_key="reviewer",
                occurred_at=_dt(5, 9, 10),
                source_url="https://api.github.com/example/events/2",
            ),
            GitHubReviewRequestSourceRecord(
                event_id=3,
                action=GitHubReviewRequestAction.REQUESTED,
                reviewer_key="reviewer",
                occurred_at=_dt(5, 10, 0),
                source_url="https://api.github.com/example/events/3",
            ),
        ),
        review_submissions=(
            GitHubReviewSubmissionSourceRecord(
                review_id=9,
                reviewer_key="reviewer",
                submitted_at=_dt(5, 10, 30),
                state="approved",
                source_url="https://api.github.com/example/reviews/9",
            ),
        ),
    )
    snapshot = GitHubPRObservationSnapshot(
        snapshot_version="request-removal",
        records=(record, _no_review_pr()),
    )

    trace = next(trace for trace in snapshot.to_dataset().traces if trace.pr_number == 1)
    assert trace.review_response_latencies_seconds() == (30 * 60.0,)


def test_review_response_coverage_is_a_preregistered_eligibility_dimension() -> None:
    snapshot = GitHubPRObservationSnapshot(
        snapshot_version="review-eligibility",
        records=(_real_pr_258(), _no_review_pr()),
    )
    criteria = EmpiricalEligibilityCriteria(
        min_pr_count=2,
        min_preterminal_ci_pr_fraction=0.0,
        min_identified_gate_pr_fraction=0.0,
        min_review_response_pr_fraction=0.5,
    )

    report = evaluate_empirical_eligibility(snapshot, criteria=criteria)

    assert report.review_response_pr_count == 1
    assert report.review_response_pr_fraction == pytest.approx(0.5)
    assert report.eligible is True

    strict = criteria.model_copy(update={"min_review_response_pr_fraction": 0.75})
    strict_report = evaluate_empirical_eligibility(snapshot, criteria=strict)
    assert strict_report.eligible is False
    assert strict_report.failed_requirements == ("min_review_response_pr_fraction",)


def test_review_source_records_are_canonical_and_validate_identity_and_time() -> None:
    requested = GitHubReviewRequestSourceRecord(
        event_id=7,
        action=GitHubReviewRequestAction.REQUESTED,
        reviewer_key="reviewer",
        occurred_at=_dt(5, 10, 0),
        source_url="https://api.github.com/example/events/7",
    )
    assert requested.canonical_payload()["action"] == "requested"

    with pytest.raises(ValidationError, match="timezone-aware"):
        GitHubReviewSubmissionSourceRecord(
            review_id=1,
            reviewer_key="reviewer",
            submitted_at=datetime(2026, 10, 5, 10, 0),
            state="approved",
            source_url="https://api.github.com/example/reviews/1",
        )

    with pytest.raises(ValidationError, match="duplicate GitHub review request event identity"):
        GitHubPRSourceRecord(
            repository=REPOSITORY,
            pr_number=2,
            opened_at=_dt(5, 9, 0),
            merged_at=_dt(5, 11, 0),
            source_url="https://api.github.com/example/pulls/2",
            review_request_events=(requested, requested),
        )

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from sose.organizational.empirical_pilot import (
    EmpiricalEligibilityCriteria,
    GitHubPRObservationSnapshot,
    GitHubPRSourceRecord,
    GitHubWorkflowJobSourceRecord,
    evaluate_empirical_eligibility,
)


UTC = timezone.utc
REPOSITORY = "brunolnetto/sose"


def _dt(day: int, hour: int, minute: int, second: int = 0) -> datetime:
    return datetime(2026, 10, day, hour, minute, second, tzinfo=UTC)


def _real_control_snapshot() -> GitHubPRObservationSnapshot:
    return GitHubPRObservationSnapshot(
        snapshot_version="sose-ci-controls@2026-10-05",
        records=(
            GitHubPRSourceRecord(
                repository=REPOSITORY,
                pr_number=258,
                opened_at=_dt(2, 11, 49, 32),
                merged_at=_dt(3, 10, 59, 35),
                source_url="https://api.github.com/repos/brunolnetto/sose/pulls/258",
                workflow_jobs=(
                    GitHubWorkflowJobSourceRecord(
                        job_id=110825193543,
                        name="coverage",
                        started_at=_dt(2, 11, 49, 39),
                        completed_at=_dt(2, 11, 52, 37),
                        conclusion="success",
                        source_url="https://api.github.com/repos/brunolnetto/sose/actions/jobs/110825193543",
                    ),
                ),
            ),
            GitHubPRSourceRecord(
                repository=REPOSITORY,
                pr_number=265,
                opened_at=_dt(5, 14, 18, 5),
                merged_at=_dt(5, 14, 18, 49),
                source_url="https://api.github.com/repos/brunolnetto/sose/pulls/265",
                workflow_jobs=(
                    GitHubWorkflowJobSourceRecord(
                        job_id=111808808473,
                        name="coverage",
                        started_at=_dt(5, 14, 18, 43),
                        completed_at=_dt(5, 14, 22, 1),
                        conclusion="success",
                        source_url="https://api.github.com/repos/brunolnetto/sose/actions/jobs/111808808473",
                    ),
                ),
            ),
        ),
    )


def test_real_controls_fail_gate_coverage_without_relabeling_observed_ci() -> None:
    criteria = EmpiricalEligibilityCriteria(
        min_pr_count=2,
        min_preterminal_ci_pr_fraction=0.5,
        min_identified_gate_pr_fraction=0.5,
    )

    report = evaluate_empirical_eligibility(_real_control_snapshot(), criteria=criteria)

    assert report.pr_count == 2
    assert report.preterminal_ci_pr_count == 1
    assert report.preterminal_ci_pr_fraction == pytest.approx(0.5)
    assert report.identified_gate_pr_count == 0
    assert report.identified_gate_pr_fraction == 0.0
    assert report.eligible is False
    assert report.failed_requirements == ("min_identified_gate_pr_fraction",)
    assert not hasattr(report, "quality_score")


def test_explicitly_provenanced_gate_can_satisfy_preregistered_threshold() -> None:
    gate_job = GitHubWorkflowJobSourceRecord(
        job_id=7,
        name="required-check",
        started_at=_dt(5, 10, 0),
        completed_at=_dt(5, 10, 2),
        conclusion="success",
        source_url="https://api.github.com/example/jobs/7",
        is_gate=True,
        gate_evidence_url="https://api.github.com/example/rulesets/1",
    )
    snapshot = GitHubPRObservationSnapshot(
        snapshot_version="eligible-control",
        records=(
            GitHubPRSourceRecord(
                repository=REPOSITORY,
                pr_number=1,
                opened_at=_dt(5, 9, 0),
                merged_at=_dt(5, 11, 0),
                source_url="https://api.github.com/example/pulls/1",
                workflow_jobs=(gate_job,),
            ),
            GitHubPRSourceRecord(
                repository=REPOSITORY,
                pr_number=2,
                opened_at=_dt(5, 9, 30),
                merged_at=_dt(5, 11, 30),
                source_url="https://api.github.com/example/pulls/2",
            ),
        ),
    )
    criteria = EmpiricalEligibilityCriteria(
        min_pr_count=2,
        min_preterminal_ci_pr_fraction=0.5,
        min_identified_gate_pr_fraction=0.5,
    )

    report = evaluate_empirical_eligibility(snapshot, criteria=criteria)

    assert report.eligible is True
    assert report.failed_requirements == ()
    assert report.preterminal_ci_pr_fraction == pytest.approx(0.5)
    assert report.identified_gate_pr_fraction == pytest.approx(0.5)


def test_eligibility_thresholds_are_explicit_and_bounded() -> None:
    with pytest.raises(ValidationError):
        EmpiricalEligibilityCriteria(
            min_pr_count=1,
            min_preterminal_ci_pr_fraction=0.0,
            min_identified_gate_pr_fraction=0.0,
        )
    with pytest.raises(ValidationError):
        EmpiricalEligibilityCriteria(
            min_pr_count=2,
            min_preterminal_ci_pr_fraction=1.1,
            min_identified_gate_pr_fraction=0.0,
        )
    with pytest.raises(ValidationError):
        EmpiricalEligibilityCriteria(
            min_pr_count=2,
            min_preterminal_ci_pr_fraction=0.0,
            min_identified_gate_pr_fraction=-0.1,
        )

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from sose.organizational.calibration import calibrate_observed_item_flow
from sose.organizational.empirical_pilot import (
    GitHubPRObservationSnapshot,
    GitHubPRSourceRecord,
    GitHubWorkflowJobSourceRecord,
    assess_snapshot_evidence,
)
from sose.organizational.observations import ObservedEventKind


UTC = timezone.utc
REPOSITORY = "brunolnetto/sose"


def _dt(year: int, month: int, day: int, hour: int, minute: int, second: int = 0) -> datetime:
    return datetime(year, month, day, hour, minute, second, tzinfo=UTC)


def test_real_preterminal_ci_is_observed_but_not_promoted_to_gate_without_provenance() -> None:
    # PR #258: the coverage job really completed before merge, so machine activity is
    # directly observed. The historical repository evidence does not establish that
    # this specific job was a required merge gate, so gate status remains unidentified.
    causal_ci = GitHubPRSourceRecord(
        repository=REPOSITORY,
        pr_number=258,
        opened_at=_dt(2026, 10, 2, 11, 49, 32),
        merged_at=_dt(2026, 10, 3, 10, 59, 35),
        source_url="https://api.github.com/repos/brunolnetto/sose/pulls/258",
        workflow_jobs=(
            GitHubWorkflowJobSourceRecord(
                job_id=110825193543,
                name="coverage",
                started_at=_dt(2026, 10, 2, 11, 49, 39),
                completed_at=_dt(2026, 10, 2, 11, 52, 37),
                conclusion="success",
                source_url="https://api.github.com/repos/brunolnetto/sose/actions/jobs/110825193543",
            ),
        ),
    )

    # PR #265: the real coverage job started before merge but completed afterwards.
    postmerge_ci = GitHubPRSourceRecord(
        repository=REPOSITORY,
        pr_number=265,
        opened_at=_dt(2026, 10, 5, 14, 18, 5),
        merged_at=_dt(2026, 10, 5, 14, 18, 49),
        source_url="https://api.github.com/repos/brunolnetto/sose/pulls/265",
        workflow_jobs=(
            GitHubWorkflowJobSourceRecord(
                job_id=111808808473,
                name="coverage",
                started_at=_dt(2026, 10, 5, 14, 18, 43),
                completed_at=_dt(2026, 10, 5, 14, 22, 1),
                conclusion="success",
                source_url="https://api.github.com/repos/brunolnetto/sose/actions/jobs/111808808473",
            ),
        ),
    )

    snapshot = GitHubPRObservationSnapshot(
        snapshot_version="ci-causality-control@2026-10-05",
        records=(causal_ci, postmerge_ci),
    )
    evidence = assess_snapshot_evidence(snapshot)

    assert evidence.pr_count == 2
    assert evidence.workflow_job_count == 2
    assert evidence.preterminal_completed_workflow_job_count == 1
    assert evidence.postterminal_completed_workflow_job_count == 1
    assert evidence.identified_preterminal_gate_job_count == 0
    assert evidence.ci_activity_observed is True
    assert evidence.ci_gate_calibratable is False

    dataset = snapshot.to_dataset()
    calibration = calibrate_observed_item_flow(dataset)
    assert calibration.ci_duration_seconds is not None
    assert calibration.ci_duration_seconds.count == 1
    assert calibration.ci_duration_seconds.mean == 178.0
    assert calibration.ci_gate_duration_seconds is None

    trace_265 = next(trace for trace in dataset.traces if trace.pr_number == 265)
    assert any(event.kind is ObservedEventKind.CI_STARTED for event in trace_265.events)
    assert not any(event.kind is ObservedEventKind.CI_COMPLETED for event in trace_265.events)


def test_explicit_gate_requires_provenance_and_enables_gate_calibration() -> None:
    gate_job = GitHubWorkflowJobSourceRecord(
        job_id=7,
        name="required-check",
        started_at=_dt(2026, 10, 5, 10, 0),
        completed_at=_dt(2026, 10, 5, 10, 2),
        conclusion="success",
        source_url="https://api.github.com/example/jobs/7",
        is_gate=True,
        gate_evidence_url="https://api.github.com/example/rulesets/1",
    )
    snapshot = GitHubPRObservationSnapshot(
        snapshot_version="declared-gate",
        records=(
            GitHubPRSourceRecord(
                repository=REPOSITORY,
                pr_number=1,
                opened_at=_dt(2026, 10, 5, 9, 0),
                merged_at=_dt(2026, 10, 5, 11, 0),
                source_url="https://api.github.com/example/pulls/1",
                workflow_jobs=(gate_job,),
            ),
            GitHubPRSourceRecord(
                repository=REPOSITORY,
                pr_number=2,
                opened_at=_dt(2026, 10, 5, 9, 30),
                merged_at=_dt(2026, 10, 5, 11, 30),
                source_url="https://api.github.com/example/pulls/2",
            ),
        ),
    )

    evidence = assess_snapshot_evidence(snapshot)
    assert evidence.identified_preterminal_gate_job_count == 1
    assert evidence.ci_gate_calibratable is True

    calibration = calibrate_observed_item_flow(snapshot.to_dataset())
    assert calibration.ci_gate_duration_seconds is not None
    assert calibration.ci_gate_duration_seconds.mean == 120.0

    with pytest.raises(ValidationError, match="gate_evidence_url"):
        GitHubWorkflowJobSourceRecord(
            job_id=8,
            name="unsupported-gate-claim",
            started_at=_dt(2026, 10, 5, 10, 0),
            completed_at=_dt(2026, 10, 5, 10, 1),
            conclusion="success",
            source_url="https://api.github.com/example/jobs/8",
            is_gate=True,
        )


def test_postmerge_only_ci_is_not_gate_calibratable() -> None:
    late_job = GitHubWorkflowJobSourceRecord(
        job_id=1,
        name="late-ci",
        started_at=_dt(2026, 10, 5, 12, 0),
        completed_at=_dt(2026, 10, 5, 12, 10),
        conclusion="success",
        source_url="https://api.github.com/example/jobs/1",
    )
    records = tuple(
        GitHubPRSourceRecord(
            repository=REPOSITORY,
            pr_number=number,
            opened_at=_dt(2026, 10, 5, 11, number - 1),
            merged_at=_dt(2026, 10, 5, 12, 5),
            source_url=f"https://api.github.com/repos/brunolnetto/sose/pulls/{number}",
            workflow_jobs=(late_job,),
        )
        for number in (1, 2)
    )
    snapshot = GitHubPRObservationSnapshot(snapshot_version="late-only", records=records)

    evidence = assess_snapshot_evidence(snapshot)
    assert evidence.preterminal_completed_workflow_job_count == 0
    assert evidence.postterminal_completed_workflow_job_count == 2
    assert evidence.ci_activity_observed is False
    assert evidence.ci_gate_calibratable is False

from __future__ import annotations

from datetime import datetime, timezone

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


def test_evidence_gate_distinguishes_preterminal_ci_from_postmerge_workflow_activity() -> None:
    # PR #258: actual coverage job from Actions run 37003141872 completed well before merge.
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

    # PR #265: actual coverage job from Actions run 37323662915 started before merge
    # but completed after merge. It is observed workflow activity, not a completed CI gate
    # available before the terminal item outcome.
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
    assert evidence.ci_calibratable is True

    dataset = snapshot.to_dataset()
    calibration = calibrate_observed_item_flow(dataset)
    assert calibration.ci_duration_seconds is not None
    assert calibration.ci_duration_seconds.count == 1
    assert calibration.ci_duration_seconds.mean == 178.0
    assert calibration.ci_gate_duration_seconds is not None
    assert calibration.ci_gate_duration_seconds.mean == 178.0

    trace_265 = next(trace for trace in dataset.traces if trace.pr_number == 265)
    assert any(event.kind is ObservedEventKind.CI_STARTED for event in trace_265.events)
    assert not any(event.kind is ObservedEventKind.CI_COMPLETED for event in trace_265.events)


def test_evidence_gate_refuses_to_call_postmerge_only_ci_calibratable() -> None:
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
    assert evidence.ci_calibratable is False

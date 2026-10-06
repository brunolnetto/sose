from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sose.organizational.observations import ObservedEventKind
from sose.organizational.source_evidence_v2 import (
    GitHubPREvidenceRecordV2,
    GitHubWorkflowJobSourceRecordV2,
)


T0 = datetime(2026, 10, 6, 15, 0, tzinfo=UTC)


def test_v2_preserves_pr_author_and_bot_identity() -> None:
    record = GitHubPREvidenceRecordV2(
        repository="brunolnetto/sose",
        pr_number=304,
        opened_at=T0,
        merged_at=T0 + timedelta(minutes=10),
        source_url="https://example.test/pulls/304",
        author_actor_key="renovate[bot]",
        author_is_bot=True,
    )

    payload = record.canonical_payload()
    assert payload["author_actor_key"] == "renovate[bot]"
    assert payload["author_is_bot"] is True

    opened = record.to_trace().events[0]
    assert opened.kind is ObservedEventKind.OPENED
    assert opened.actor_key == "renovate[bot]"
    assert opened.metadata["author_is_bot"] is True


def test_v2_workflow_jobs_preserve_workflow_run_and_attempt_identity() -> None:
    job = GitHubWorkflowJobSourceRecordV2(
        job_id=9001,
        workflow_id=44,
        run_id=7001,
        run_attempt=2,
        name="coverage",
        started_at=T0 + timedelta(minutes=1),
        completed_at=T0 + timedelta(minutes=4),
        conclusion="success",
        source_url="https://example.test/actions/jobs/9001",
    )
    record = GitHubPREvidenceRecordV2(
        repository="brunolnetto/sose",
        pr_number=304,
        opened_at=T0,
        merged_at=T0 + timedelta(minutes=10),
        source_url="https://example.test/pulls/304",
        workflow_jobs=(job,),
    )

    job_payload = record.canonical_payload()["workflow_jobs"][0]
    assert job_payload["workflow_id"] == 44
    assert job_payload["run_id"] == 7001
    assert job_payload["run_attempt"] == 2

    ci_started = next(event for event in record.to_trace().events if event.kind is ObservedEventKind.CI_STARTED)
    assert ci_started.metadata["workflow_id"] == 44
    assert ci_started.metadata["run_id"] == 7001
    assert ci_started.metadata["run_attempt"] == 2

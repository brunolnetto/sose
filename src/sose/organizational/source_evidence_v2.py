from __future__ import annotations

from datetime import datetime
from hashlib import sha256
import json
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

from .dataset import ObservedPRDataset
from .empirical_pilot import GitHubWorkflowJobSourceRecord
from .github_observations import normalize_github_pr_trace


NonBlankString = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
SOURCE_EVIDENCE_VERSION = "pr-review-source-evidence/v2"


def _require_aware(value: datetime, *, field_name: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must be timezone-aware")


class GitHubReviewTimelineSourceRecord(BaseModel):
    """Durable source identity for review-request timeline evidence."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    event_id: int = Field(ge=1)
    event: Literal["review_requested", "review_request_removed"]
    occurred_at: datetime
    source_url: NonBlankString
    requested_actor_key: NonBlankString | None = None

    @model_validator(mode="after")
    def validate_timestamp(self) -> "GitHubReviewTimelineSourceRecord":
        _require_aware(self.occurred_at, field_name="occurred_at")
        return self

    def canonical_payload(self) -> dict[str, object]:
        return {
            "event_id": self.event_id,
            "event": self.event,
            "occurred_at": self.occurred_at.isoformat(),
            "source_url": self.source_url,
            "requested_actor_key": self.requested_actor_key,
        }

    def adapter_payload(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "id": self.event_id,
            "event": self.event,
            "created_at": self.occurred_at.isoformat(),
        }
        actor = self.requested_actor_key
        if actor is not None:
            if actor.startswith("team:"):
                payload["requested_team"] = {"slug": actor.removeprefix("team:")}
            else:
                payload["requested_reviewer"] = {"login": actor}
        return payload


class GitHubReviewSourceRecord(BaseModel):
    """Durable source identity for one submitted GitHub review."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    review_id: int = Field(ge=1)
    submitted_at: datetime
    state: NonBlankString
    actor_key: NonBlankString | None = None
    source_url: NonBlankString

    @model_validator(mode="after")
    def validate_timestamp(self) -> "GitHubReviewSourceRecord":
        _require_aware(self.submitted_at, field_name="submitted_at")
        return self

    def canonical_payload(self) -> dict[str, object]:
        return {
            "review_id": self.review_id,
            "submitted_at": self.submitted_at.isoformat(),
            "state": self.state,
            "actor_key": self.actor_key,
            "source_url": self.source_url,
        }

    def adapter_payload(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "id": self.review_id,
            "submitted_at": self.submitted_at.isoformat(),
            "state": self.state,
        }
        if self.actor_key is not None:
            payload["user"] = {"login": self.actor_key}
        return payload


class GitHubPREvidenceRecordV2(BaseModel):
    """Review-aware source record for the prospective v2 PR study.

    This is deliberately separate from the v1 `GitHubPRSourceRecord`; extending
    the v1 canonical payload would invalidate already frozen snapshot hashes.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    repository: NonBlankString
    pr_number: int = Field(ge=1)
    opened_at: datetime
    merged_at: datetime
    source_url: NonBlankString
    workflow_jobs: tuple[GitHubWorkflowJobSourceRecord, ...] = ()
    review_timeline: tuple[GitHubReviewTimelineSourceRecord, ...] = ()
    submitted_reviews: tuple[GitHubReviewSourceRecord, ...] = ()

    @model_validator(mode="after")
    def canonicalize_and_validate(self) -> "GitHubPREvidenceRecordV2":
        _require_aware(self.opened_at, field_name="opened_at")
        _require_aware(self.merged_at, field_name="merged_at")
        if self.merged_at < self.opened_at:
            raise ValueError("merged_at must be at or after opened_at")

        job_ids = tuple(job.job_id for job in self.workflow_jobs)
        if len(job_ids) != len(set(job_ids)):
            raise ValueError("duplicate GitHub workflow job identity")
        timeline_ids = tuple(event.event_id for event in self.review_timeline)
        if len(timeline_ids) != len(set(timeline_ids)):
            raise ValueError("duplicate GitHub review timeline identity")
        review_ids = tuple(review.review_id for review in self.submitted_reviews)
        if len(review_ids) != len(set(review_ids)):
            raise ValueError("duplicate GitHub review identity")

        object.__setattr__(
            self,
            "workflow_jobs",
            tuple(sorted(self.workflow_jobs, key=lambda job: (job.started_at, job.job_id))),
        )
        object.__setattr__(
            self,
            "review_timeline",
            tuple(sorted(self.review_timeline, key=lambda event: (event.occurred_at, event.event_id))),
        )
        object.__setattr__(
            self,
            "submitted_reviews",
            tuple(sorted(self.submitted_reviews, key=lambda review: (review.submitted_at, review.review_id))),
        )
        return self

    def canonical_payload(self) -> dict[str, object]:
        return {
            "repository": self.repository,
            "pr_number": self.pr_number,
            "opened_at": self.opened_at.isoformat(),
            "merged_at": self.merged_at.isoformat(),
            "source_url": self.source_url,
            "workflow_jobs": [job.canonical_payload() for job in self.workflow_jobs],
            "review_timeline": [event.canonical_payload() for event in self.review_timeline],
            "submitted_reviews": [review.canonical_payload() for review in self.submitted_reviews],
        }

    def to_trace(self):
        return normalize_github_pr_trace(
            repository=self.repository,
            pull_request={
                "number": self.pr_number,
                "created_at": self.opened_at.isoformat(),
                "merged_at": self.merged_at.isoformat(),
                "closed_at": self.merged_at.isoformat(),
            },
            timeline_events=tuple(event.adapter_payload() for event in self.review_timeline),
            reviews=tuple(review.adapter_payload() for review in self.submitted_reviews),
            workflow_jobs=tuple(job.adapter_payload() for job in self.workflow_jobs),
        )


class GitHubPREvidenceSnapshotV2(BaseModel):
    """Canonical hash-addressed prospective source snapshot with review evidence."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    snapshot_version: Literal[SOURCE_EVIDENCE_VERSION] = SOURCE_EVIDENCE_VERSION
    records: tuple[GitHubPREvidenceRecordV2, ...] = Field(min_length=2)

    @model_validator(mode="after")
    def canonicalize_and_validate(self) -> "GitHubPREvidenceSnapshotV2":
        keys = tuple((record.repository, record.pr_number) for record in self.records)
        if len(keys) != len(set(keys)):
            raise ValueError("duplicate GitHub pull request identity")
        object.__setattr__(
            self,
            "records",
            tuple(
                sorted(
                    self.records,
                    key=lambda record: (record.opened_at, record.repository, record.pr_number),
                )
            ),
        )
        return self

    def canonical_payload(self) -> dict[str, object]:
        return {
            "snapshot_version": self.snapshot_version,
            "records": [record.canonical_payload() for record in self.records],
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
    def snapshot_hash(self) -> str:
        return sha256(self.canonical_json().encode("utf-8")).hexdigest()

    def to_dataset(self) -> ObservedPRDataset:
        return ObservedPRDataset(
            dataset_version=self.snapshot_version,
            traces=tuple(record.to_trace() for record in self.records),
        )

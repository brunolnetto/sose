from __future__ import annotations

from datetime import datetime
from hashlib import sha256
import json
from typing import Annotated, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    field_validator,
    model_validator,
)

from .dataset import ObservedPRDataset
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

    @field_validator("event", mode="before")
    @classmethod
    def validate_event(cls, value: object) -> object:
        if value not in {"review_requested", "review_request_removed"}:
            raise ValueError("event must be review_requested or review_request_removed")
        return value

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


class GitHubWorkflowJobSourceRecordV2(BaseModel):
    """Review-study workflow evidence preserving workflow/run provenance.

    The v1 workflow-job schema is intentionally left frozen because it is part of
    an already hash-addressed source contract.  The prospective v2 study needs
    workflow and run identities so reruns/retries can be grouped independently of
    individual job identities.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    job_id: int = Field(ge=1)
    workflow_id: int = Field(ge=1)
    run_id: int = Field(ge=1)
    run_attempt: int = Field(ge=1)
    name: NonBlankString
    started_at: datetime
    completed_at: datetime
    conclusion: NonBlankString
    source_url: NonBlankString
    is_gate: bool = False
    gate_evidence_url: NonBlankString | None = None

    @model_validator(mode="after")
    def validate_interval(self) -> "GitHubWorkflowJobSourceRecordV2":
        _require_aware(self.started_at, field_name="started_at")
        _require_aware(self.completed_at, field_name="completed_at")
        if self.completed_at < self.started_at:
            raise ValueError("completed_at must be at or after started_at")
        if self.is_gate and self.gate_evidence_url is None:
            raise ValueError("gate_evidence_url is required when is_gate is true")
        if not self.is_gate and self.gate_evidence_url is not None:
            raise ValueError("gate_evidence_url requires is_gate=true")
        return self

    def canonical_payload(self) -> dict[str, object]:
        return {
            "job_id": self.job_id,
            "workflow_id": self.workflow_id,
            "run_id": self.run_id,
            "run_attempt": self.run_attempt,
            "name": self.name,
            "started_at": self.started_at.isoformat(),
            "completed_at": self.completed_at.isoformat(),
            "conclusion": self.conclusion,
            "source_url": self.source_url,
            "is_gate": self.is_gate,
            "gate_evidence_url": self.gate_evidence_url,
        }

    def adapter_payload(self) -> dict[str, object]:
        return {
            "id": self.job_id,
            "workflow_id": self.workflow_id,
            "run_id": self.run_id,
            "run_attempt": self.run_attempt,
            "name": self.name,
            "started_at": self.started_at.isoformat(),
            "completed_at": self.completed_at.isoformat(),
            "conclusion": self.conclusion,
            "is_gate": self.is_gate,
            "gate_evidence_url": self.gate_evidence_url,
        }


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
    author_actor_key: NonBlankString | None = None
    author_is_bot: bool = False
    workflow_jobs: tuple[GitHubWorkflowJobSourceRecordV2, ...] = ()
    review_timeline: tuple[GitHubReviewTimelineSourceRecord, ...] = ()
    submitted_reviews: tuple[GitHubReviewSourceRecord, ...] = ()

    @model_validator(mode="after")
    def canonicalize_and_validate(self) -> "GitHubPREvidenceRecordV2":
        _require_aware(self.opened_at, field_name="opened_at")
        _require_aware(self.merged_at, field_name="merged_at")
        if self.merged_at < self.opened_at:
            raise ValueError("merged_at must be at or after opened_at")
        if self.author_is_bot and self.author_actor_key is None:
            raise ValueError("author_actor_key is required when author_is_bot is true")

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
            tuple(
                sorted(
                    self.workflow_jobs,
                    key=lambda job: (
                        job.started_at,
                        job.workflow_id,
                        job.run_id,
                        job.run_attempt,
                        job.job_id,
                    ),
                )
            ),
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
            "author_actor_key": self.author_actor_key,
            "author_is_bot": self.author_is_bot,
            "workflow_jobs": [job.canonical_payload() for job in self.workflow_jobs],
            "review_timeline": [event.canonical_payload() for event in self.review_timeline],
            "submitted_reviews": [review.canonical_payload() for review in self.submitted_reviews],
        }

    def to_trace(self):
        pull_request: dict[str, object] = {
            "number": self.pr_number,
            "created_at": self.opened_at.isoformat(),
            "merged_at": self.merged_at.isoformat(),
            "closed_at": self.merged_at.isoformat(),
            "author_is_bot": self.author_is_bot,
        }
        if self.author_actor_key is not None:
            pull_request["user"] = {"login": self.author_actor_key}
        return normalize_github_pr_trace(
            repository=self.repository,
            pull_request=pull_request,
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

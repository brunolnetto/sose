from __future__ import annotations

from datetime import datetime
from hashlib import sha256
import json
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

from .dataset import ObservedPRDataset, ObservedPRKey
from .github_observations import normalize_github_pr_trace
from .heldout_prediction import (
    PRReviewAssumptions,
    PRReviewHeldoutPrediction,
    predict_pr_review_holdout,
)


NonBlankString = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]


def _require_aware(value: datetime, *, field_name: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must be timezone-aware")


class GitHubWorkflowJobSourceRecord(BaseModel):
    """Direct machine-side workflow-job evidence with durable source identity.

    `is_gate` is stronger than observing a workflow job. It may only be asserted
    when a separate source identifies that job as part of the merge gate.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    job_id: int = Field(ge=1)
    name: NonBlankString
    started_at: datetime
    completed_at: datetime
    conclusion: NonBlankString
    source_url: NonBlankString
    is_gate: bool = False
    gate_evidence_url: NonBlankString | None = None

    @model_validator(mode="after")
    def validate_interval(self) -> "GitHubWorkflowJobSourceRecord":
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
            "name": self.name,
            "started_at": self.started_at.isoformat(),
            "completed_at": self.completed_at.isoformat(),
            "conclusion": self.conclusion,
            "is_gate": self.is_gate,
            "gate_evidence_url": self.gate_evidence_url,
        }


class GitHubPRSourceRecord(BaseModel):
    """Source-preserving GitHub evidence for one merged PR lifecycle.

    Item timestamps and machine-side workflow intervals may be recorded directly.
    Human effort, availability, meetings, interruptions and utilization are never
    reconstructed from gaps between these events.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    repository: NonBlankString
    pr_number: int = Field(ge=1)
    opened_at: datetime
    merged_at: datetime
    source_url: NonBlankString
    workflow_jobs: tuple[GitHubWorkflowJobSourceRecord, ...] = ()

    @model_validator(mode="after")
    def validate_lifecycle(self) -> "GitHubPRSourceRecord":
        _require_aware(self.opened_at, field_name="opened_at")
        _require_aware(self.merged_at, field_name="merged_at")
        if self.merged_at < self.opened_at:
            raise ValueError("merged_at must be at or after opened_at")
        job_ids = tuple(job.job_id for job in self.workflow_jobs)
        if len(job_ids) != len(set(job_ids)):
            raise ValueError("duplicate GitHub workflow job identity")
        ordered = tuple(sorted(self.workflow_jobs, key=lambda job: (job.started_at, job.job_id)))
        object.__setattr__(self, "workflow_jobs", ordered)
        return self

    def canonical_payload(self) -> dict[str, object]:
        return {
            "repository": self.repository,
            "pr_number": self.pr_number,
            "opened_at": self.opened_at.isoformat(),
            "merged_at": self.merged_at.isoformat(),
            "source_url": self.source_url,
            "workflow_jobs": [job.canonical_payload() for job in self.workflow_jobs],
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
            workflow_jobs=tuple(job.adapter_payload() for job in self.workflow_jobs),
        )


class GitHubPRObservationSnapshot(BaseModel):
    """Canonical, hash-addressed collection of observed merged GitHub PR records."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    snapshot_version: NonBlankString
    records: tuple[GitHubPRSourceRecord, ...] = Field(min_length=2)

    @model_validator(mode="after")
    def canonicalize_and_validate(self) -> "GitHubPRObservationSnapshot":
        keys = [(record.repository, record.pr_number) for record in self.records]
        if len(keys) != len(set(keys)):
            raise ValueError("duplicate GitHub pull request identity")
        ordered = tuple(
            sorted(
                self.records,
                key=lambda record: (record.opened_at, record.repository, record.pr_number),
            )
        )
        object.__setattr__(self, "records", ordered)
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


class EmpiricalSourceEvidence(BaseModel):
    """Non-composite data-quality facts for one source snapshot."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    pr_count: int = Field(ge=1)
    workflow_job_count: int = Field(ge=0)
    preterminal_completed_workflow_job_count: int = Field(ge=0)
    postterminal_completed_workflow_job_count: int = Field(ge=0)
    identified_preterminal_gate_job_count: int = Field(ge=0)
    ci_activity_observed: bool
    ci_gate_calibratable: bool


def assess_snapshot_evidence(snapshot: GitHubPRObservationSnapshot) -> EmpiricalSourceEvidence:
    """Separate observed machine activity from independently identified merge-gate evidence."""

    workflow_job_count = 0
    preterminal_completed = 0
    postterminal_completed = 0
    identified_preterminal_gate = 0
    for record in snapshot.records:
        workflow_job_count += len(record.workflow_jobs)
        for job in record.workflow_jobs:
            if job.completed_at <= record.merged_at:
                preterminal_completed += 1
                if job.is_gate:
                    identified_preterminal_gate += 1
            else:
                postterminal_completed += 1

    return EmpiricalSourceEvidence(
        pr_count=len(snapshot.records),
        workflow_job_count=workflow_job_count,
        preterminal_completed_workflow_job_count=preterminal_completed,
        postterminal_completed_workflow_job_count=postterminal_completed,
        identified_preterminal_gate_job_count=identified_preterminal_gate,
        ci_activity_observed=preterminal_completed > 0,
        ci_gate_calibratable=identified_preterminal_gate > 0,
    )


class PRReviewEmpiricalPilotResult(BaseModel):
    """Auditable empirical-pilot result bound to source, split and model hashes."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    snapshot_hash: NonBlankString
    train_dataset_hash: NonBlankString
    holdout_dataset_hash: NonBlankString
    train_keys: tuple[ObservedPRKey, ...]
    holdout_keys: tuple[ObservedPRKey, ...]
    purged_keys: tuple[ObservedPRKey, ...]
    prediction: PRReviewHeldoutPrediction


def run_pr_review_empirical_pilot(
    *,
    snapshot: GitHubPRObservationSnapshot,
    holdout_fraction: float,
    assumptions: PRReviewAssumptions,
    seed: int,
) -> PRReviewEmpiricalPilotResult:
    """Run the first source-bound, leakage-free PR-review empirical pilot."""

    dataset = snapshot.to_dataset()
    split = dataset.chronological_holdout(holdout_fraction=holdout_fraction)
    prediction = predict_pr_review_holdout(
        split=split,
        assumptions=assumptions,
        seed=seed,
    )
    return PRReviewEmpiricalPilotResult(
        snapshot_hash=snapshot.snapshot_hash,
        train_dataset_hash=split.train.dataset_hash,
        holdout_dataset_hash=split.holdout.dataset_hash,
        train_keys=split.train.keys,
        holdout_keys=split.holdout.keys,
        purged_keys=split.purged_keys,
        prediction=prediction,
    )

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


class GitHubPRSourceRecord(BaseModel):
    """Minimal source-preserving GitHub evidence for a merged PR lifecycle.

    This record deliberately contains only directly observed item-lifecycle evidence.
    It does not infer reviewer effort, actor availability, meetings, or interruptions.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    repository: NonBlankString
    pr_number: int = Field(ge=1)
    opened_at: datetime
    merged_at: datetime
    source_url: NonBlankString

    @model_validator(mode="after")
    def validate_lifecycle(self) -> "GitHubPRSourceRecord":
        for field_name, value in (("opened_at", self.opened_at), ("merged_at", self.merged_at)):
            if value.tzinfo is None or value.utcoffset() is None:
                raise ValueError(f"{field_name} must be timezone-aware")
        if self.merged_at < self.opened_at:
            raise ValueError("merged_at must be at or after opened_at")
        return self

    def canonical_payload(self) -> dict[str, object]:
        return {
            "repository": self.repository,
            "pr_number": self.pr_number,
            "opened_at": self.opened_at.isoformat(),
            "merged_at": self.merged_at.isoformat(),
            "source_url": self.source_url,
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

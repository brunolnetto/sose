"""Wave 0 O2: provenance-bounded PR review observability, not human service time.

This evaluator operates on already normalized *offline* GitHub source traces.
A criteria hash and asserted registration time do NOT independently prove
preregistration; a separately timestamped registration must be reviewed before
any scientific eligibility claim. This API only reports source-observability.
"""
from __future__ import annotations

from collections import defaultdict, deque
from datetime import datetime
from hashlib import sha256
import json
import re
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

from .dataset import ObservedPRDataset
from .observations import ObservedEventKind, ObservedPRTrace


NonBlank = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
_REQUEST_ID = re.compile(r"^github:timeline:[1-9][0-9]*:review_requested$")
_REMOVED_ID = re.compile(r"^github:timeline:[1-9][0-9]*:review_request_removed$")
_REVIEW_ID = re.compile(r"^github:review:[1-9][0-9]*$")


class ReviewSourceEligibilityCriteria(BaseModel):
    """Prospective, *externally registered* requirements for source coverage.

    Passing a digest alone never demonstrates when a criterion was registered.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    registration_source: NonBlank
    registered_at: datetime
    min_train_pr_count: int = Field(ge=1)
    min_paired_review_pr_fraction: float = Field(gt=0.0, le=1.0, allow_inf_nan=False)
    holdout_fraction: float = Field(gt=0.0, lt=1.0, allow_inf_nan=False)

    @model_validator(mode="after")
    def require_timezone(self) -> "ReviewSourceEligibilityCriteria":
        if self.registered_at.tzinfo is None or self.registered_at.utcoffset() is None:
            raise ValueError("registration time must be timezone-aware")
        return self

    @property
    def criteria_hash(self) -> str:
        payload = json.dumps(
            self.model_dump(mode="json"),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        )
        return sha256(payload.encode("utf-8")).hexdigest()


class ReviewSourceEligibilityReport(BaseModel):
    """Observed training-only source coverage; NOT empirical/scientific admission."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    dataset_hash: NonBlank
    criteria_hash: NonBlank
    training_dataset_hash: NonBlank
    holdout_dataset_hash: NonBlank
    train_pr_count: int = Field(ge=1)
    paired_review_pr_count: int = Field(ge=0)
    paired_review_pr_fraction: float = Field(ge=0.0, le=1.0, allow_inf_nan=False)
    holdout_pr_count: int = Field(ge=1)
    purged_pr_count: int = Field(ge=0)
    failed_requirements: tuple[NonBlank, ...]
    source_observability_eligible: bool


def has_source_paired_review(trace: ObservedPRTrace) -> bool:
    """Require a source-identified request and same-actor submitted review.

    A removed request cancels the oldest outstanding request from that actor.
    Team requests and anonymous actors never become individual observations.
    All source IDs must be GitHub-origin-shaped (a requirement, not external
    authentication). No latency is interpreted as reviewer service effort.
    """
    pending: dict[str, deque[datetime]] = defaultdict(deque)
    paired = False
    for event in trace.events:
        if not _is_review_source_event(event):
            continue
        actor = event.actor_key
        if actor is None or actor.startswith("team:"):
            continue
        # No early return: later events must still pass source validation.
        paired = _apply_review_event(pending[actor], event) or paired
    return paired


_REVIEW_SOURCE_RULES = {
    ObservedEventKind.REVIEW_REQUESTED: (
        _REQUEST_ID, "review request lacks a valid GitHub source identity",
    ),
    ObservedEventKind.REVIEW_REQUEST_REMOVED: (
        _REMOVED_ID, "removed review request lacks GitHub source identity",
    ),
    ObservedEventKind.REVIEW_SUBMITTED: (
        _REVIEW_ID, "submitted review lacks a valid GitHub source identity",
    ),
}


def _is_review_source_event(event) -> bool:
    """True for review events (whose source ID must be GitHub-shaped)."""
    rule = _REVIEW_SOURCE_RULES.get(event.kind)
    if rule is None:
        return False
    pattern, message = rule
    if not pattern.fullmatch(event.source_event_id):
        raise ValueError(message)
    return True


def _apply_review_event(actor_pending: deque[datetime], event) -> bool:
    """Update one actor's outstanding requests; True when a review pairs."""
    if event.kind is ObservedEventKind.REVIEW_REQUESTED:
        actor_pending.append(event.occurred_at)
        return False
    if event.kind is ObservedEventKind.REVIEW_REQUEST_REMOVED:
        if actor_pending:
            actor_pending.popleft()
        return False
    if actor_pending:
        return event.occurred_at > actor_pending.popleft()
    return False


def evaluate_review_source_eligibility(
    dataset: ObservedPRDataset,
    *,
    criteria: ReviewSourceEligibilityCriteria,
    registered_criteria_hash: str,
    expected_dataset_hash: str,
) -> ReviewSourceEligibilityReport:
    """Use a sealed dataset and separate hash inputs, never scored holdout data.

    The caller MUST verify the registration_source from an independently
    timestamped immutable record; supplying a matching hash here is not proof.
    """
    if criteria.criteria_hash != registered_criteria_hash:
        raise ValueError("criteria hash differs from the registered value")
    if dataset.dataset_hash != expected_dataset_hash:
        raise ValueError("observed dataset hash differs from the registered value")
    if criteria.registered_at > dataset.traces[0].opened_at:
        raise ValueError("claimed criteria registration postdates source cohort")


    # GitHub source event IDs are global observations, not new IDs per PR.
    # A repeated review/request identity in different PRs is corrupted
    # attribution even if the entire corrupted dataset was hash-pinned.
    _assert_unique_review_sources(dataset)

    split = dataset.chronological_holdout(holdout_fraction=criteria.holdout_fraction)
    count = len(split.train.traces)
    paired = sum(has_source_paired_review(trace) for trace in split.train.traces)
    fraction = paired / count
    failed: list[str] = []
    if count < criteria.min_train_pr_count:
        failed.append("min_train_pr_count")
    if fraction < criteria.min_paired_review_pr_fraction:
        failed.append("min_paired_review_pr_fraction")

    return ReviewSourceEligibilityReport(
        dataset_hash=dataset.dataset_hash,
        criteria_hash=criteria.criteria_hash,
        training_dataset_hash=split.train.dataset_hash,
        holdout_dataset_hash=split.holdout.dataset_hash,
        train_pr_count=count,
        paired_review_pr_count=paired,
        paired_review_pr_fraction=fraction,
        holdout_pr_count=len(split.holdout.traces),
        purged_pr_count=len(split.purged_keys),
        failed_requirements=tuple(failed),
        source_observability_eligible=not failed,
    )


def _assert_unique_review_sources(dataset: ObservedPRDataset) -> None:
    source_owners: dict[str, tuple[str, int]] = {}
    for trace in dataset.traces:
        owner = (trace.repository, trace.pr_number)
        for event in trace.events:
            if event.kind not in _REVIEW_SOURCE_RULES:
                continue
            previous = source_owners.setdefault(event.source_event_id, owner)
            if previous != owner:
                raise ValueError("review source identity reused across pull requests")

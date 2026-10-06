from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

from .dataset import ObservedPRKey
from .source_evidence_v2 import GitHubPREvidenceRecordV2


class ProspectiveCohortStatus(StrEnum):
    COLLECTING_TRAINING = "collecting_training"
    AWAITING_MODEL_FREEZE = "awaiting_model_freeze"
    COLLECTING_HOLDOUT = "collecting_holdout"
    COMPLETE = "complete"


class ProspectivePRCohortV2(BaseModel):
    """Persistable enrollment state for the preregistered v2 study."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    repository: str
    registration_merged_at: datetime
    model_frozen_at: datetime | None = None
    training_count: int = Field(ge=1)
    holdout_count: int = Field(ge=1)
    status: ProspectiveCohortStatus
    training_keys: tuple[ObservedPRKey, ...]
    interstitial_keys: tuple[ObservedPRKey, ...]
    holdout_keys: tuple[ObservedPRKey, ...]
    post_holdout_keys: tuple[ObservedPRKey, ...]
    training_completed_at: datetime | None = None


def select_prospective_pr_cohort_v2(
    *,
    records: tuple[GitHubPREvidenceRecordV2, ...] | list[GitHubPREvidenceRecordV2],
    registration_merged_at: datetime,
    training_count: int,
    holdout_count: int,
    repository: str = "brunolnetto/sose",
    model_frozen_at: datetime | None = None,
    previous_cohort: ProspectivePRCohortV2 | None = None,
) -> ProspectivePRCohortV2:
    """Advance deterministic prospective enrollment without rewriting prior cohorts.

    The first projection orders currently observed eligible merged PRs by creation
    time. Every subsequent projection may supply the previously persisted cohort;
    already enrolled identities are then immutable. This prevents an earlier-opened
    PR that merges late from displacing training or holdout items whose outcomes may
    already have informed model design or validation.
    """

    _require_aware(registration_merged_at, field_name="registration_merged_at")
    if model_frozen_at is not None:
        _require_aware(model_frozen_at, field_name="model_frozen_at")
    if not repository.strip():
        raise ValueError("repository must be non-blank")
    if training_count < 1:
        raise ValueError("training_count must be at least one")
    if holdout_count < 1:
        raise ValueError("holdout_count must be at least one")

    if previous_cohort is not None:
        _validate_previous(
            previous_cohort,
            repository=repository,
            registration_merged_at=registration_merged_at,
            training_count=training_count,
            holdout_count=holdout_count,
        )
        if previous_cohort.model_frozen_at is not None:
            if model_frozen_at is None:
                model_frozen_at = previous_cohort.model_frozen_at
            elif model_frozen_at != previous_cohort.model_frozen_at:
                raise ValueError("model_frozen_at cannot change after it is persisted")

    ordered = tuple(
        sorted(
            (
                record
                for record in records
                if record.repository == repository and record.opened_at > registration_merged_at
            ),
            key=lambda record: (record.opened_at, record.pr_number),
        )
    )
    keys = tuple((record.repository, record.pr_number) for record in ordered)
    if len(keys) != len(set(keys)):
        raise ValueError("duplicate GitHub pull request identity")
    by_key = {(record.repository, record.pr_number): record for record in ordered}

    previous_training = previous_cohort.training_keys if previous_cohort is not None else ()
    previous_interstitial = previous_cohort.interstitial_keys if previous_cohort is not None else ()
    previous_holdout = previous_cohort.holdout_keys if previous_cohort is not None else ()
    previous_post = previous_cohort.post_holdout_keys if previous_cohort is not None else ()
    persisted_keys = set(
        (*previous_training, *previous_interstitial, *previous_holdout, *previous_post)
    )
    missing_persisted = persisted_keys.difference(by_key)
    if missing_persisted:
        raise ValueError("records must retain every previously enrolled pull request")

    new_records = tuple(
        record for record in ordered if (record.repository, record.pr_number) not in persisted_keys
    )

    training_keys = list(previous_training)
    for record in new_records:
        if len(training_keys) >= training_count:
            break
        training_keys.append((record.repository, record.pr_number))

    if len(training_keys) < training_count:
        if model_frozen_at is not None:
            raise ValueError("cannot freeze v2 model before training cohort is complete")
        return ProspectivePRCohortV2(
            repository=repository,
            registration_merged_at=registration_merged_at,
            training_count=training_count,
            holdout_count=holdout_count,
            status=ProspectiveCohortStatus.COLLECTING_TRAINING,
            training_keys=tuple(training_keys),
            interstitial_keys=previous_interstitial,
            holdout_keys=previous_holdout,
            post_holdout_keys=previous_post,
        )

    training_records = tuple(by_key[key] for key in training_keys)
    if previous_cohort is not None and previous_cohort.training_completed_at is not None:
        training_completed_at = previous_cohort.training_completed_at
    else:
        training_completed_at = max(record.merged_at for record in training_records)

    consumed_training = set(training_keys)
    remaining_new = tuple(
        record
        for record in new_records
        if (record.repository, record.pr_number) not in consumed_training
    )

    if model_frozen_at is None:
        interstitial_keys = _append_unique(previous_interstitial, _keys(remaining_new))
        return ProspectivePRCohortV2(
            repository=repository,
            registration_merged_at=registration_merged_at,
            training_count=training_count,
            holdout_count=holdout_count,
            status=ProspectiveCohortStatus.AWAITING_MODEL_FREEZE,
            training_keys=tuple(training_keys),
            interstitial_keys=interstitial_keys,
            holdout_keys=previous_holdout,
            post_holdout_keys=previous_post,
            training_completed_at=training_completed_at,
        )

    if model_frozen_at < training_completed_at:
        raise ValueError("model_frozen_at must be at or after training completion")

    interstitial_new = tuple(record for record in remaining_new if record.opened_at <= model_frozen_at)
    after_freeze_new = tuple(record for record in remaining_new if record.opened_at > model_frozen_at)
    interstitial_keys = _append_unique(previous_interstitial, _keys(interstitial_new))

    holdout_keys = list(previous_holdout)
    post_keys = list(previous_post)
    for record in after_freeze_new:
        key = (record.repository, record.pr_number)
        if len(holdout_keys) < holdout_count:
            holdout_keys.append(key)
        else:
            post_keys.append(key)

    status = (
        ProspectiveCohortStatus.COMPLETE
        if len(holdout_keys) == holdout_count
        else ProspectiveCohortStatus.COLLECTING_HOLDOUT
    )

    return ProspectivePRCohortV2(
        repository=repository,
        registration_merged_at=registration_merged_at,
        model_frozen_at=model_frozen_at,
        training_count=training_count,
        holdout_count=holdout_count,
        status=status,
        training_keys=tuple(training_keys),
        interstitial_keys=interstitial_keys,
        holdout_keys=tuple(holdout_keys),
        post_holdout_keys=tuple(post_keys),
        training_completed_at=training_completed_at,
    )


def _validate_previous(
    previous: ProspectivePRCohortV2,
    *,
    repository: str,
    registration_merged_at: datetime,
    training_count: int,
    holdout_count: int,
) -> None:
    if previous.repository != repository:
        raise ValueError("previous cohort repository does not match")
    if previous.registration_merged_at != registration_merged_at:
        raise ValueError("previous cohort registration timestamp does not match")
    if previous.training_count != training_count or previous.holdout_count != holdout_count:
        raise ValueError("previous cohort sizes do not match")


def _append_unique(
    existing: tuple[ObservedPRKey, ...],
    added: tuple[ObservedPRKey, ...],
) -> tuple[ObservedPRKey, ...]:
    seen = set(existing)
    result = list(existing)
    for key in added:
        if key not in seen:
            result.append(key)
            seen.add(key)
    return tuple(result)


def _keys(records: tuple[GitHubPREvidenceRecordV2, ...]) -> tuple[ObservedPRKey, ...]:
    return tuple((record.repository, record.pr_number) for record in records)


def _require_aware(value: datetime, *, field_name: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must be timezone-aware")

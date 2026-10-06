from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from sose.organizational.prospective_cohort_v2 import (
    ProspectiveCohortStatus,
    select_prospective_pr_cohort_v2,
)
from sose.organizational.source_evidence_v2 import GitHubPREvidenceRecordV2


REGISTERED_AT = datetime(2026, 10, 6, 15, 0, tzinfo=UTC)


def test_training_uses_first_18_merged_prs_opened_strictly_after_registration_merge() -> None:
    before = _record(300, opened=REGISTERED_AT - timedelta(seconds=1), duration=60)
    equal = _record(301, opened=REGISTERED_AT, duration=60)
    future = tuple(
        _record(302 + index, opened=REGISTERED_AT + timedelta(minutes=index + 1), duration=60)
        for index in range(20)
    )

    cohort = select_prospective_pr_cohort_v2(
        records=(before, equal, *reversed(future)),
        registration_merged_at=REGISTERED_AT,
        training_count=18,
        holdout_count=12,
    )

    assert cohort.status is ProspectiveCohortStatus.AWAITING_MODEL_FREEZE
    assert cohort.training_keys == tuple(("brunolnetto/sose", number) for number in range(302, 320))
    assert cohort.interstitial_keys == (
        ("brunolnetto/sose", 320),
        ("brunolnetto/sose", 321),
    )
    assert cohort.holdout_keys == ()


def test_partial_training_never_exposes_a_holdout() -> None:
    records = tuple(
        _record(302 + index, opened=REGISTERED_AT + timedelta(minutes=index + 1), duration=60)
        for index in range(5)
    )

    cohort = select_prospective_pr_cohort_v2(
        records=records,
        registration_merged_at=REGISTERED_AT,
        training_count=18,
        holdout_count=12,
    )

    assert cohort.status is ProspectiveCohortStatus.COLLECTING_TRAINING
    assert len(cohort.training_keys) == 5
    assert cohort.holdout_keys == ()
    assert cohort.interstitial_keys == ()


def test_after_model_freeze_only_newly_opened_prs_can_enter_holdout() -> None:
    training = tuple(
        _record(302 + index, opened=REGISTERED_AT + timedelta(minutes=index + 1), duration=60)
        for index in range(18)
    )
    training_completed = max(record.merged_at for record in training)
    between = (
        _record(320, opened=training_completed + timedelta(minutes=1), duration=60),
        _record(321, opened=training_completed + timedelta(minutes=2), duration=60),
    )
    model_frozen_at = training_completed + timedelta(minutes=10)
    holdout = tuple(
        _record(322 + index, opened=model_frozen_at + timedelta(minutes=index + 1), duration=60)
        for index in range(14)
    )

    cohort = select_prospective_pr_cohort_v2(
        records=(*training, *between, *holdout),
        registration_merged_at=REGISTERED_AT,
        training_count=18,
        holdout_count=12,
        model_frozen_at=model_frozen_at,
    )

    assert cohort.status is ProspectiveCohortStatus.COMPLETE
    assert cohort.interstitial_keys == (
        ("brunolnetto/sose", 320),
        ("brunolnetto/sose", 321),
    )
    assert cohort.holdout_keys == tuple(("brunolnetto/sose", number) for number in range(322, 334))
    assert cohort.post_holdout_keys == (
        ("brunolnetto/sose", 334),
        ("brunolnetto/sose", 335),
    )


def test_model_freeze_before_training_completion_is_rejected() -> None:
    training = tuple(
        _record(302 + index, opened=REGISTERED_AT + timedelta(minutes=index + 1), duration=120)
        for index in range(18)
    )
    too_early = max(record.merged_at for record in training) - timedelta(seconds=1)

    with pytest.raises(ValueError, match="model_frozen_at must be at or after training completion"):
        select_prospective_pr_cohort_v2(
            records=training,
            registration_merged_at=REGISTERED_AT,
            training_count=18,
            holdout_count=12,
            model_frozen_at=too_early,
        )


def test_model_freeze_cannot_be_declared_before_training_is_complete() -> None:
    records = tuple(
        _record(302 + index, opened=REGISTERED_AT + timedelta(minutes=index + 1), duration=60)
        for index in range(17)
    )

    with pytest.raises(ValueError, match="cannot freeze v2 model before training cohort is complete"):
        select_prospective_pr_cohort_v2(
            records=records,
            registration_merged_at=REGISTERED_AT,
            training_count=18,
            holdout_count=12,
            model_frozen_at=REGISTERED_AT + timedelta(days=1),
        )


def test_timestamps_must_be_timezone_aware() -> None:
    with pytest.raises(ValueError, match="registration_merged_at must be timezone-aware"):
        select_prospective_pr_cohort_v2(
            records=(),
            registration_merged_at=datetime(2026, 10, 6, 15, 0),
            training_count=18,
            holdout_count=12,
        )


def _record(number: int, *, opened: datetime, duration: int) -> GitHubPREvidenceRecordV2:
    return GitHubPREvidenceRecordV2(
        repository="brunolnetto/sose",
        pr_number=number,
        opened_at=opened,
        merged_at=opened + timedelta(seconds=duration),
        source_url=f"https://api.github.com/repos/brunolnetto/sose/pulls/{number}",
    )

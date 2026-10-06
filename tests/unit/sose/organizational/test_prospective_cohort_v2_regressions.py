from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sose.organizational.prospective_cohort_v2 import (
    ProspectiveCohortStatus,
    select_prospective_pr_cohort_v2,
)
from sose.organizational.source_evidence_v2 import GitHubPREvidenceRecordV2


REGISTERED_AT = datetime(2026, 10, 6, 15, 0, tzinfo=UTC)
REPOSITORY = "brunolnetto/sose"


def test_late_merge_cannot_displace_persisted_training_enrollment() -> None:
    second = _record(303, opened=REGISTERED_AT + timedelta(minutes=2), merged_after=2)
    third = _record(304, opened=REGISTERED_AT + timedelta(minutes=3), merged_after=2)
    initial = select_prospective_pr_cohort_v2(
        records=(second, third),
        repository=REPOSITORY,
        registration_merged_at=REGISTERED_AT,
        training_count=2,
        holdout_count=2,
    )
    assert initial.status is ProspectiveCohortStatus.AWAITING_MODEL_FREEZE
    assert initial.training_keys == ((REPOSITORY, 303), (REPOSITORY, 304))

    late_first = _record(302, opened=REGISTERED_AT + timedelta(minutes=1), merged_after=60)
    replayed = select_prospective_pr_cohort_v2(
        records=(late_first, second, third),
        repository=REPOSITORY,
        registration_merged_at=REGISTERED_AT,
        training_count=2,
        holdout_count=2,
        previous_cohort=initial,
    )

    assert replayed.training_keys == initial.training_keys
    assert replayed.interstitial_keys == ((REPOSITORY, 302),)


def test_late_merge_cannot_displace_persisted_holdout_enrollment() -> None:
    training = _record(302, opened=REGISTERED_AT + timedelta(minutes=1), merged_after=1)
    awaiting_freeze = select_prospective_pr_cohort_v2(
        records=(training,),
        repository=REPOSITORY,
        registration_merged_at=REGISTERED_AT,
        training_count=1,
        holdout_count=2,
    )
    freeze = training.merged_at + timedelta(minutes=5)

    holdout_a = _record(304, opened=freeze + timedelta(minutes=2), merged_after=1)
    holdout_b = _record(305, opened=freeze + timedelta(minutes=3), merged_after=1)
    complete = select_prospective_pr_cohort_v2(
        records=(training, holdout_a, holdout_b),
        repository=REPOSITORY,
        registration_merged_at=REGISTERED_AT,
        training_count=1,
        holdout_count=2,
        model_frozen_at=freeze,
        previous_cohort=awaiting_freeze,
    )
    assert complete.status is ProspectiveCohortStatus.COMPLETE
    assert complete.holdout_keys == ((REPOSITORY, 304), (REPOSITORY, 305))

    late_earlier = _record(303, opened=freeze + timedelta(minutes=1), merged_after=60)
    replayed = select_prospective_pr_cohort_v2(
        records=(training, late_earlier, holdout_a, holdout_b),
        repository=REPOSITORY,
        registration_merged_at=REGISTERED_AT,
        training_count=1,
        holdout_count=2,
        model_frozen_at=freeze,
        previous_cohort=complete,
    )

    assert replayed.holdout_keys == complete.holdout_keys
    assert replayed.post_holdout_keys == ((REPOSITORY, 303),)


def test_other_repositories_never_enter_registered_cohort() -> None:
    foreign = GitHubPREvidenceRecordV2(
        repository="other/project",
        pr_number=1,
        opened_at=REGISTERED_AT + timedelta(minutes=1),
        merged_at=REGISTERED_AT + timedelta(minutes=2),
        source_url="https://example.test/other/project/pulls/1",
    )
    local = _record(302, opened=REGISTERED_AT + timedelta(minutes=2), merged_after=1)

    cohort = select_prospective_pr_cohort_v2(
        records=(foreign, local),
        repository=REPOSITORY,
        registration_merged_at=REGISTERED_AT,
        training_count=1,
        holdout_count=1,
    )

    assert cohort.training_keys == ((REPOSITORY, 302),)


def _record(number: int, *, opened: datetime, merged_after: int) -> GitHubPREvidenceRecordV2:
    return GitHubPREvidenceRecordV2(
        repository=REPOSITORY,
        pr_number=number,
        opened_at=opened,
        merged_at=opened + timedelta(minutes=merged_after),
        source_url=f"https://api.github.com/repos/{REPOSITORY}/pulls/{number}",
    )

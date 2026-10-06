from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone

import pytest

from sose.organizational.prospective_protocol_v2 import (
    ProspectiveStudyProtocolV2,
    bind_pr_review_validation_protocol_v2,
)


REGISTERED_AT = datetime(2026, 10, 6, 14, 44, 13, tzinfo=UTC)


def test_protocol_binding_is_canonical_and_hash_addressed() -> None:
    left = bind_pr_review_validation_protocol_v2(
        document=_document(),
        registration_merged_at=REGISTERED_AT,
    )
    reordered = dict(reversed(tuple(_document().items())))
    right = bind_pr_review_validation_protocol_v2(
        document=reordered,
        registration_merged_at=REGISTERED_AT,
    )

    assert left == right
    assert left.protocol_id == "pr-review-validation/v2"
    assert left.repository == "brunolnetto/sose"
    assert left.registration_pr_number == 301
    assert left.training_count == 18
    assert left.holdout_count == 12
    assert len(left.protocol_document_hash) == 64
    assert len(left.protocol_hash) == 64


def test_activation_timestamp_is_part_of_protocol_identity() -> None:
    baseline = bind_pr_review_validation_protocol_v2(
        document=_document(),
        registration_merged_at=REGISTERED_AT,
    )
    later = bind_pr_review_validation_protocol_v2(
        document=_document(),
        registration_merged_at=REGISTERED_AT + timedelta(seconds=1),
    )

    assert baseline.protocol_document_hash == later.protocol_document_hash
    assert baseline.protocol_hash != later.protocol_hash


def test_same_activation_instant_has_same_hash_across_timezone_offsets() -> None:
    utc = bind_pr_review_validation_protocol_v2(
        document=_document(),
        registration_merged_at=REGISTERED_AT,
    )
    same_instant = REGISTERED_AT.astimezone(timezone(timedelta(hours=-4)))
    offset = bind_pr_review_validation_protocol_v2(
        document=_document(),
        registration_merged_at=same_instant,
    )

    assert utc.registration_merged_at == offset.registration_merged_at
    assert utc.protocol_hash == offset.protocol_hash


def test_protocol_binding_rejects_wrong_frozen_contract_shape() -> None:
    wrong_repository = _document()
    wrong_repository["repository"] = "other/project"
    with pytest.raises(ValueError, match="registered repository"):
        bind_pr_review_validation_protocol_v2(
            document=wrong_repository,
            registration_merged_at=REGISTERED_AT,
        )

    wrong_counts = _document()
    wrong_counts["cohort"] = {**wrong_counts["cohort"], "training_count": 17}
    with pytest.raises(ValueError, match="training_count=18"):
        bind_pr_review_validation_protocol_v2(
            document=wrong_counts,
            registration_merged_at=REGISTERED_AT,
        )


def test_protocol_model_rejects_naive_registration_merge_timestamp() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        ProspectiveStudyProtocolV2(
            protocol_document_hash="a" * 64,
            repository="brunolnetto/sose",
            registration_pr_number=301,
            registration_merged_at=datetime(2026, 10, 6, 14, 44, 13),
            training_count=18,
            holdout_count=12,
        )


def _document() -> dict[str, object]:
    return {
        "protocol_version": "pr-review-validation/v2",
        "study_type": "prospective_two_stage",
        "repository": "brunolnetto/sose",
        "registration_pr_number": 301,
        "activation_rule": "pull requests created strictly after registration PR merge",
        "cohort": {
            "training_count": 18,
            "holdout_count": 12,
            "selection": "first eligible merged pull requests in creation-time order",
        },
        "stage_rules": {
            "holdout_enrollment_before_model_freeze": False,
            "v2_model_may_use_training_only": True,
            "holdout_outcomes_may_change_model": False,
        },
    }

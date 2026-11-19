"""#590: falsify every distinct frozen prospective v2 admission predicate.

A preregistered holdout may never influence model training. Each variant is
built from the same valid study document, changing exactly one immutable gate
and requiring a *specific* failure instead of generic validation success.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import UTC, datetime
from hashlib import sha256
import json

import pytest

from sose.organizational.prospective_protocol_v2 import (
    ACTIVATION_RULE,
    HOLDOUT_COUNT,
    PROSPECTIVE_PROTOCOL_ID,
    PROSPECTIVE_STUDY_TYPE,
    REGISTERED_REPOSITORY,
    REGISTRATION_PR_NUMBER,
    SELECTION_RULE,
    TRAINING_COUNT,
    bind_pr_review_validation_protocol_v2,
)

REGISTRATION = datetime(2026, 10, 6, 14, 44, 13, tzinfo=UTC)


def _frozen_document():
    return {
        "protocol_version": PROSPECTIVE_PROTOCOL_ID,
        "study_type": PROSPECTIVE_STUDY_TYPE,
        "repository": REGISTERED_REPOSITORY,
        "registration_pr_number": REGISTRATION_PR_NUMBER,
        "activation_rule": ACTIVATION_RULE,
        "cohort": {
            "training_count": TRAINING_COUNT,
            "holdout_count": HOLDOUT_COUNT,
            "selection": SELECTION_RULE,
        },
        "stage_rules": {
            "holdout_enrollment_before_model_freeze": False,
            "v2_model_may_use_training_only": True,
            "holdout_outcomes_may_change_model": False,
        },
    }


@pytest.mark.parametrize(
    ("path", "mutant", "error"),
    [
        (("protocol_version",), "pr-review-validation/v1", "protocol_version must be"),
        (("study_type",), "retroactive", "study_type must be"),
        (("repository",), "forged/another", "registered repository must be"),
        (("registration_pr_number",), 302, "registration_pr_number must be"),
        (("activation_rule",), "any open PR", "activation_rule does not match"),
        (("cohort",), None, "cohort must be an object"),
        (("cohort", "training_count"), 17, "training_count=18"),
        (("cohort", "holdout_count"), 11, "holdout_count=12"),
        (("cohort", "selection"), "latest convenient cases", "cohort selection does not match"),
        (("stage_rules",), None, "stage_rules must be an object"),
        (
            ("stage_rules", "holdout_enrollment_before_model_freeze"),
            True, "holdout enrollment must remain disabled",
        ),
        (
            ("stage_rules", "v2_model_may_use_training_only"),
            False, "v2 model must use training evidence only",
        ),
        (
            ("stage_rules", "holdout_outcomes_may_change_model"),
            True, "holdout outcomes must not change",
        ),
    ],
)
def test_frozen_protocol_refuses_one_unauthorized_semantic_change(path, mutant, error):
    document = _frozen_document()
    target = document
    for segment in path[:-1]:
        target = target[segment]
    target[path[-1]] = mutant
    before = deepcopy(document)

    with pytest.raises(ValueError, match=error):
        bind_pr_review_validation_protocol_v2(
            document=document, registration_merged_at=REGISTRATION,
        )

    # Admission must not silently rewrite an invalid preregistered source.
    assert document == before


def test_valid_frozen_document_binds_immutable_activation_without_mutating_source():
    document = _frozen_document()
    before = deepcopy(document)
    protocol = bind_pr_review_validation_protocol_v2(
        document=document, registration_merged_at=REGISTRATION,
    )
    assert document == before
    canonical = json.dumps(
        document, sort_keys=True, separators=(",", ":"),
        ensure_ascii=False, allow_nan=False,
    )
    assert protocol.protocol_document_hash == sha256(canonical.encode()).hexdigest()
    assert protocol.canonical_payload()["registration_merged_at"] == REGISTRATION.isoformat()
    assert protocol.protocol_hash == sha256(protocol.canonical_json().encode()).hexdigest()

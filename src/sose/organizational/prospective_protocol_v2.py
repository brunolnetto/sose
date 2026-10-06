from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from hashlib import sha256
import json
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, StringConstraints, model_validator


PROSPECTIVE_PROTOCOL_ID = "pr-review-validation/v2"
PROSPECTIVE_STUDY_TYPE = "prospective_two_stage"
REGISTERED_REPOSITORY = "brunolnetto/sose"
REGISTRATION_PR_NUMBER = 301
TRAINING_COUNT = 18
HOLDOUT_COUNT = 12
ACTIVATION_RULE = "pull requests created strictly after registration PR merge"
SELECTION_RULE = "first eligible merged pull requests in creation-time order"
Sha256Hex = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]


class ProspectiveStudyProtocolV2(BaseModel):
    """Hash-addressed activation envelope for the frozen prospective v2 protocol.

    ``protocol_document_hash`` identifies the exact canonical preregistration
    document supplied by the caller. ``protocol_hash`` additionally binds the
    GitHub registration merge timestamp that activates prospective enrollment.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    protocol_id: Literal[PROSPECTIVE_PROTOCOL_ID] = PROSPECTIVE_PROTOCOL_ID
    protocol_document_hash: Sha256Hex
    repository: Literal[REGISTERED_REPOSITORY] = REGISTERED_REPOSITORY
    registration_pr_number: Literal[REGISTRATION_PR_NUMBER] = REGISTRATION_PR_NUMBER
    registration_merged_at: datetime
    training_count: Literal[TRAINING_COUNT] = TRAINING_COUNT
    holdout_count: Literal[HOLDOUT_COUNT] = HOLDOUT_COUNT

    @model_validator(mode="after")
    def validate_activation(self) -> "ProspectiveStudyProtocolV2":
        if self.registration_merged_at.tzinfo is None or self.registration_merged_at.utcoffset() is None:
            raise ValueError("registration_merged_at must be timezone-aware")
        return self

    def canonical_payload(self) -> dict[str, object]:
        return {
            "protocol_id": self.protocol_id,
            "protocol_document_hash": self.protocol_document_hash,
            "repository": self.repository,
            "registration_pr_number": self.registration_pr_number,
            "registration_merged_at": self.registration_merged_at.isoformat(),
            "training_count": self.training_count,
            "holdout_count": self.holdout_count,
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
    def protocol_hash(self) -> str:
        return sha256(self.canonical_json().encode("utf-8")).hexdigest()


def bind_pr_review_validation_protocol_v2(
    *,
    document: Mapping[str, Any],
    registration_merged_at: datetime,
) -> ProspectiveStudyProtocolV2:
    """Bind the frozen preregistration document to its GitHub activation event."""

    _validate_frozen_contract(document)
    canonical_document = json.dumps(
        document,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )
    return ProspectiveStudyProtocolV2(
        protocol_document_hash=sha256(canonical_document.encode("utf-8")).hexdigest(),
        registration_merged_at=registration_merged_at,
    )


def _validate_frozen_contract(document: Mapping[str, Any]) -> None:
    if document.get("protocol_version") != PROSPECTIVE_PROTOCOL_ID:
        raise ValueError(f"protocol_version must be {PROSPECTIVE_PROTOCOL_ID}")
    if document.get("study_type") != PROSPECTIVE_STUDY_TYPE:
        raise ValueError(f"study_type must be {PROSPECTIVE_STUDY_TYPE}")
    if document.get("repository") != REGISTERED_REPOSITORY:
        raise ValueError(f"registered repository must be {REGISTERED_REPOSITORY}")
    if document.get("registration_pr_number") != REGISTRATION_PR_NUMBER:
        raise ValueError(f"registration_pr_number must be {REGISTRATION_PR_NUMBER}")
    if document.get("activation_rule") != ACTIVATION_RULE:
        raise ValueError("activation_rule does not match the frozen v2 contract")

    cohort = document.get("cohort")
    if not isinstance(cohort, Mapping):
        raise ValueError("cohort must be an object")
    if cohort.get("training_count") != TRAINING_COUNT:
        raise ValueError(f"prospective v2 requires training_count={TRAINING_COUNT}")
    if cohort.get("holdout_count") != HOLDOUT_COUNT:
        raise ValueError(f"prospective v2 requires holdout_count={HOLDOUT_COUNT}")
    if cohort.get("selection") != SELECTION_RULE:
        raise ValueError("cohort selection does not match the frozen v2 contract")

    stage_rules = document.get("stage_rules")
    if not isinstance(stage_rules, Mapping):
        raise ValueError("stage_rules must be an object")
    if stage_rules.get("holdout_enrollment_before_model_freeze") is not False:
        raise ValueError("holdout enrollment must remain disabled before model freeze")
    if stage_rules.get("v2_model_may_use_training_only") is not True:
        raise ValueError("v2 model must use training evidence only")
    if stage_rules.get("holdout_outcomes_may_change_model") is not False:
        raise ValueError("holdout outcomes must not change the v2 model")

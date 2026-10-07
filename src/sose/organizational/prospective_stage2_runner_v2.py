from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
import json
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

from .acquisition_union_v2 import merge_acquisition_snapshots_v2
from .acquisition_v2 import GitHubPRAcquisitionSnapshotV2
from .dataset import ObservedPRKey
from .prospective_cohort_v2 import ProspectiveCohortStatus
from .prospective_model_freeze_v2 import ProspectiveModelFreezeArtifactV2
from .prospective_state_v2 import (
    ProspectiveEvidenceStateV2,
    advance_prospective_evidence_state_v2,
)
from .source_evidence_v2 import GitHubPREvidenceSnapshotV2


STAGE2_CHECKPOINT_VERSION_V2 = "pr-review-prospective-stage2-checkpoint/v2"
Sha256Hex = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]


class ProspectiveStage2CheckpointV2(BaseModel):
    """Hash-addressed progress gate for the untouched prospective holdout."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    checkpoint_version: Literal[STAGE2_CHECKPOINT_VERSION_V2] = STAGE2_CHECKPOINT_VERSION_V2
    state_hash: Sha256Hex
    protocol_hash: Sha256Hex
    snapshot_hash: Sha256Hex
    previous_state_hash: Sha256Hex | None
    model_freeze_hash: Sha256Hex
    holdout_observed: int = Field(ge=0)
    holdout_target: int = Field(ge=1)
    holdout_remaining: int = Field(ge=0)
    holdout_keys: tuple[ObservedPRKey, ...]
    interstitial_count: int = Field(ge=0)
    post_holdout_count: int = Field(ge=0)
    complete: bool
    validation_allowed: bool

    @model_validator(mode="after")
    def validate_progress(self) -> "ProspectiveStage2CheckpointV2":
        if len(self.holdout_keys) != len(set(self.holdout_keys)):
            raise ValueError("holdout_keys must be unique")
        if self.holdout_observed != len(self.holdout_keys):
            raise ValueError("holdout_observed must match holdout_keys")
        if self.holdout_observed > self.holdout_target:
            raise ValueError("holdout_observed cannot exceed holdout_target")
        expected_remaining = self.holdout_target - self.holdout_observed
        if self.holdout_remaining != expected_remaining:
            raise ValueError("holdout_remaining must equal holdout_target - holdout_observed")
        expected_complete = expected_remaining == 0
        if self.complete is not expected_complete:
            raise ValueError("complete must exactly reflect Stage-2 holdout completion")
        if self.validation_allowed is not expected_complete:
            raise ValueError("validation_allowed must be true only at complete holdout")
        if self.post_holdout_count != 0:
            raise ValueError("Stage-2 checkpoint cannot expose post-holdout evidence")
        return self

    def canonical_payload(self) -> dict[str, object]:
        return {
            "checkpoint_version": self.checkpoint_version,
            "state_hash": self.state_hash,
            "protocol_hash": self.protocol_hash,
            "snapshot_hash": self.snapshot_hash,
            "previous_state_hash": self.previous_state_hash,
            "model_freeze_hash": self.model_freeze_hash,
            "holdout_observed": self.holdout_observed,
            "holdout_target": self.holdout_target,
            "holdout_remaining": self.holdout_remaining,
            "holdout_keys": [list(key) for key in self.holdout_keys],
            "interstitial_count": self.interstitial_count,
            "post_holdout_count": self.post_holdout_count,
            "complete": self.complete,
            "validation_allowed": self.validation_allowed,
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
    def checkpoint_hash(self) -> str:
        return sha256(self.canonical_json().encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class ProspectiveStage2AdvancementV2:
    acquisition: GitHubPRAcquisitionSnapshotV2
    state: ProspectiveEvidenceStateV2
    checkpoint: ProspectiveStage2CheckpointV2


def advance_stage2_artifacts_v2(
    *,
    model_freeze: ProspectiveModelFreezeArtifactV2,
    tranche: GitHubPRAcquisitionSnapshotV2,
    previous_acquisition: GitHubPRAcquisitionSnapshotV2,
    previous_state: ProspectiveEvidenceStateV2,
    previous_checkpoint: ProspectiveStage2CheckpointV2 | None = None,
) -> ProspectiveStage2AdvancementV2:
    """Append one Stage-2 tranche without permitting model changes or holdout overflow."""

    _validate_freeze_binding(model_freeze=model_freeze, state=previous_state)
    if previous_state.snapshot_hash == model_freeze.snapshot_hash:
        if previous_state.previous_state_hash != model_freeze.training_state_hash:
            raise ValueError("freeze artifact does not bind to exact pre-freeze state")
        if previous_checkpoint is not None:
            raise ValueError("previous Stage-2 checkpoint is not valid before first Stage-2 advancement")
    else:
        if previous_checkpoint is None:
            raise ValueError("previous Stage-2 checkpoint is required after Stage-2 has started")
        expected_previous = build_stage2_checkpoint_v2(
            state=previous_state,
            model_freeze=model_freeze,
        )
        if previous_checkpoint.checkpoint_hash != expected_previous.checkpoint_hash:
            raise ValueError("previous Stage-2 checkpoint does not match previous state")
        if previous_checkpoint.model_freeze_hash != model_freeze.freeze_hash:
            raise ValueError("previous Stage-2 checkpoint does not bind the exact model freeze")

    if previous_state.cohort.status is ProspectiveCohortStatus.COMPLETE:
        raise ValueError("Stage-2 holdout is already complete")
    if previous_state.cohort.status is not ProspectiveCohortStatus.COLLECTING_HOLDOUT:
        raise ValueError("Stage-2 advancement requires collecting_holdout state")

    cumulative = merge_acquisition_snapshots_v2(previous_acquisition, tranche)
    state = advance_prospective_evidence_state_v2(
        records=cumulative.to_evidence_snapshot().records,
        protocol=previous_state.protocol,
        model_frozen_at=model_freeze.frozen_at,
        previous_state=previous_state,
    )
    if state.cohort.post_holdout_keys:
        raise ValueError("Stage-2 tranche exceeds preregistered holdout target")

    checkpoint = build_stage2_checkpoint_v2(
        state=state,
        model_freeze=model_freeze,
    )
    return ProspectiveStage2AdvancementV2(
        acquisition=cumulative,
        state=state,
        checkpoint=checkpoint,
    )


def build_stage2_checkpoint_v2(
    *,
    state: ProspectiveEvidenceStateV2,
    model_freeze: ProspectiveModelFreezeArtifactV2,
) -> ProspectiveStage2CheckpointV2:
    _validate_freeze_binding(model_freeze=model_freeze, state=state)
    cohort = state.cohort
    if cohort.status not in {
        ProspectiveCohortStatus.COLLECTING_HOLDOUT,
        ProspectiveCohortStatus.COMPLETE,
    }:
        raise ValueError("Stage-2 checkpoint requires a post-freeze cohort")
    if cohort.post_holdout_keys:
        raise ValueError("Stage-2 checkpoint cannot include post-holdout evidence")

    observed = len(cohort.holdout_keys)
    target = cohort.holdout_count
    remaining = target - observed
    complete = remaining == 0
    return ProspectiveStage2CheckpointV2(
        state_hash=state.state_hash,
        protocol_hash=state.protocol_hash,
        snapshot_hash=state.snapshot_hash,
        previous_state_hash=state.previous_state_hash,
        model_freeze_hash=model_freeze.freeze_hash,
        holdout_observed=observed,
        holdout_target=target,
        holdout_remaining=remaining,
        holdout_keys=cohort.holdout_keys,
        interstitial_count=len(cohort.interstitial_keys),
        post_holdout_count=len(cohort.post_holdout_keys),
        complete=complete,
        validation_allowed=complete,
    )


def _validate_freeze_binding(
    *,
    model_freeze: ProspectiveModelFreezeArtifactV2,
    state: ProspectiveEvidenceStateV2,
) -> None:
    cohort = state.cohort
    if state.protocol_hash != model_freeze.protocol_hash:
        raise ValueError("freeze artifact does not bind to prospective state protocol")
    if cohort.model_frozen_at != model_freeze.frozen_at:
        raise ValueError("freeze artifact does not bind to prospective state freeze timestamp")

    pre_freeze_records = tuple(
        record
        for record in state.snapshot.records
        if record.opened_at <= model_freeze.frozen_at
    )
    pre_freeze_keys = {
        (record.repository, record.pr_number)
        for record in pre_freeze_records
    }
    if not set(cohort.training_keys).issubset(pre_freeze_keys):
        raise ValueError("freeze artifact does not bind to complete training evidence")
    pre_freeze_snapshot = GitHubPREvidenceSnapshotV2(records=pre_freeze_records)
    if pre_freeze_snapshot.snapshot_hash != model_freeze.snapshot_hash:
        raise ValueError("freeze artifact does not bind to prospective state pre-freeze snapshot")

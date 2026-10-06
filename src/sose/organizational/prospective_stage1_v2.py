from __future__ import annotations

from hashlib import sha256
import json
import os
from pathlib import Path
import tempfile
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

from .dataset import ObservedPRKey
from .prospective_cohort_v2 import ProspectiveCohortStatus
from .prospective_state_v2 import ProspectiveEvidenceStateV2


STAGE1_CHECKPOINT_VERSION = "pr-review-prospective-stage1-checkpoint/v2"
Sha256Hex = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]


class ProspectiveStage1ReadinessCheckpointV2(BaseModel):
    """Hash-addressed readiness view over one immutable prospective evidence state.

    The checkpoint intentionally carries no holdout identities. It is a Stage-1
    audit artifact: it says how much preregistered training evidence is enrolled
    and whether model freeze is permitted by the frozen cohort contract.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    checkpoint_version: Literal[STAGE1_CHECKPOINT_VERSION] = STAGE1_CHECKPOINT_VERSION
    state_hash: Sha256Hex
    protocol_hash: Sha256Hex
    snapshot_hash: Sha256Hex
    previous_state_hash: Sha256Hex | None = None
    training_observed: int = Field(ge=0)
    training_target: int = Field(ge=1)
    training_remaining: int = Field(ge=0)
    training_keys: tuple[ObservedPRKey, ...]
    interstitial_count: int = Field(ge=0)
    freeze_allowed: bool
    holdout_exposed: Literal[False] = False

    @model_validator(mode="after")
    def validate_readiness_derivations(self) -> "ProspectiveStage1ReadinessCheckpointV2":
        if len(self.training_keys) != len(set(self.training_keys)):
            raise ValueError("training_keys must be unique")
        if self.training_observed != len(self.training_keys):
            raise ValueError("training_observed must match training_keys")
        if self.training_observed > self.training_target:
            raise ValueError("training_observed cannot exceed training_target")
        expected_remaining = self.training_target - self.training_observed
        if self.training_remaining != expected_remaining:
            raise ValueError("training_remaining must equal training_target - training_observed")
        expected_freeze_allowed = expected_remaining == 0
        if self.freeze_allowed is not expected_freeze_allowed:
            raise ValueError("freeze_allowed must exactly reflect Stage-1 training completion")
        return self

    def canonical_payload(self) -> dict[str, object]:
        return {
            "checkpoint_version": self.checkpoint_version,
            "state_hash": self.state_hash,
            "protocol_hash": self.protocol_hash,
            "snapshot_hash": self.snapshot_hash,
            "previous_state_hash": self.previous_state_hash,
            "training_observed": self.training_observed,
            "training_target": self.training_target,
            "training_remaining": self.training_remaining,
            "training_keys": [list(key) for key in self.training_keys],
            "interstitial_count": self.interstitial_count,
            "freeze_allowed": self.freeze_allowed,
            "holdout_exposed": self.holdout_exposed,
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


def build_stage1_readiness_checkpoint_v2(
    state: ProspectiveEvidenceStateV2,
) -> ProspectiveStage1ReadinessCheckpointV2:
    """Project a pre-freeze evidence state into an auditable Stage-1 checkpoint."""

    cohort = state.cohort
    if cohort.model_frozen_at is not None or cohort.status not in {
        ProspectiveCohortStatus.COLLECTING_TRAINING,
        ProspectiveCohortStatus.AWAITING_MODEL_FREEZE,
    }:
        raise ValueError("Stage-1 readiness checkpoints are only valid before model freeze")
    if cohort.holdout_keys or cohort.post_holdout_keys:
        raise ValueError("Stage-1 readiness checkpoint cannot contain holdout evidence")

    observed = len(cohort.training_keys)
    target = cohort.training_count
    if observed > target:
        raise ValueError("training enrollment exceeds preregistered target")
    remaining = target - observed
    freeze_allowed = (
        remaining == 0
        and cohort.status is ProspectiveCohortStatus.AWAITING_MODEL_FREEZE
    )

    return ProspectiveStage1ReadinessCheckpointV2(
        state_hash=state.state_hash,
        protocol_hash=state.protocol_hash,
        snapshot_hash=state.snapshot_hash,
        previous_state_hash=state.previous_state_hash,
        training_observed=observed,
        training_target=target,
        training_remaining=remaining,
        training_keys=cohort.training_keys,
        interstitial_count=len(cohort.interstitial_keys),
        freeze_allowed=freeze_allowed,
    )


def publish_stage1_readiness_checkpoint_v2(
    *,
    state: ProspectiveEvidenceStateV2,
    output_path: str | Path,
) -> ProspectiveStage1ReadinessCheckpointV2:
    """Atomically publish a Stage-1 checkpoint without permitting replacement."""

    checkpoint = build_stage1_readiness_checkpoint_v2(state)
    serialized = checkpoint.canonical_json() + "\n"
    output_file = Path(output_path)
    output_file.parent.mkdir(parents=True, exist_ok=True)

    if output_file.exists():
        _accept_identical_or_raise(output_file, serialized)
        return checkpoint

    descriptor, temporary_name = tempfile.mkstemp(
        dir=output_file.parent,
        prefix=f".{output_file.name}.",
        suffix=".tmp",
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(serialized)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.link(temporary, output_file)
        except FileExistsError:
            _accept_identical_or_raise(output_file, serialized)
    finally:
        temporary.unlink(missing_ok=True)
    return checkpoint


def _accept_identical_or_raise(output_file: Path, serialized: str) -> None:
    if output_file.read_text(encoding="utf-8") == serialized:
        return
    raise FileExistsError(f"output already contains a different artifact: {output_file}")

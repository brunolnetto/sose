from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from hashlib import sha256
import json
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

from .model_spec import ModelSpec
from .prospective_stage1_v2 import ProspectiveStage1ReadinessCheckpointV2
from .prospective_state_v2 import (
    ProspectiveEvidenceStateV2,
    advance_prospective_evidence_state_v2,
)
from .validation import LeadTimeValidationCriteria


MODEL_FREEZE_VERSION_V2 = "pr-review-prospective-model-freeze/v2"
SEED_POLICY_V2 = "counter_keyed_item_mechanism"
REQUIRED_TAIL_METRICS_V2 = (
    "mean",
    "median",
    "p90",
    "maximum",
    "ecdf_distance",
    "max_to_median_ratio",
)

NonBlankString = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
Sha256Hex = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]
TailMetric = Literal[
    "mean",
    "median",
    "p90",
    "maximum",
    "ecdf_distance",
    "max_to_median_ratio",
]


class ProspectiveModelFreezeArtifactV2(BaseModel):
    """Immutable analysis contract frozen between Stage 1 and Stage 2."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    freeze_version: Literal[MODEL_FREEZE_VERSION_V2] = MODEL_FREEZE_VERSION_V2
    readiness_checkpoint_hash: Sha256Hex
    training_state_hash: Sha256Hex
    protocol_hash: Sha256Hex
    snapshot_hash: Sha256Hex
    frozen_at: datetime
    model_spec: ModelSpec
    model_spec_hash: Sha256Hex
    simulation_seed: int = Field(ge=0)
    seed_policy: Literal[SEED_POLICY_V2] = SEED_POLICY_V2
    acceptance_criteria: LeadTimeValidationCriteria
    acceptance_criteria_hash: Sha256Hex
    tail_metrics: tuple[TailMetric, ...]
    source_normalization_rules: NonBlankString
    missing_data_policy: NonBlankString
    fitting_rule: NonBlankString

    @model_validator(mode="after")
    def validate_derivations(self) -> "ProspectiveModelFreezeArtifactV2":
        if self.frozen_at.tzinfo is None or self.frozen_at.utcoffset() is None:
            raise ValueError("frozen_at must be timezone-aware")
        if self.model_spec_hash != self.model_spec.model_spec_hash:
            raise ValueError("model_spec_hash must match the frozen ModelSpec")
        if self.acceptance_criteria_hash != self.acceptance_criteria.criteria_hash:
            raise ValueError("acceptance_criteria_hash must match frozen acceptance criteria")
        if self.tail_metrics != REQUIRED_TAIL_METRICS_V2:
            raise ValueError("tail metrics must exactly match the preregistered v2 minimum set")
        return self

    def canonical_payload(self) -> dict[str, object]:
        return {
            "freeze_version": self.freeze_version,
            "readiness_checkpoint_hash": self.readiness_checkpoint_hash,
            "training_state_hash": self.training_state_hash,
            "protocol_hash": self.protocol_hash,
            "snapshot_hash": self.snapshot_hash,
            "frozen_at": self.frozen_at.isoformat(),
            "model_spec": self.model_spec.canonical_payload(),
            "model_spec_hash": self.model_spec_hash,
            "simulation_seed": self.simulation_seed,
            "seed_policy": self.seed_policy,
            "acceptance_criteria": self.acceptance_criteria.model_dump(mode="json"),
            "acceptance_criteria_hash": self.acceptance_criteria_hash,
            "tail_metrics": list(self.tail_metrics),
            "source_normalization_rules": self.source_normalization_rules,
            "missing_data_policy": self.missing_data_policy,
            "fitting_rule": self.fitting_rule,
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
    def freeze_hash(self) -> str:
        return sha256(self.canonical_json().encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class ProspectiveModelFreezeResultV2:
    artifact: ProspectiveModelFreezeArtifactV2
    frozen_state: ProspectiveEvidenceStateV2


def freeze_prospective_model_v2(
    *,
    state: ProspectiveEvidenceStateV2,
    readiness: ProspectiveStage1ReadinessCheckpointV2,
    frozen_at: datetime,
    model_spec: ModelSpec,
    simulation_seed: int,
    acceptance_criteria: LeadTimeValidationCriteria,
    tail_metrics: tuple[TailMetric, ...],
    source_normalization_rules: str,
    missing_data_policy: str,
    fitting_rule: str,
) -> ProspectiveModelFreezeResultV2:
    """Freeze the v2 model only after the preregistered Stage-1 gate is satisfied."""

    if not readiness.freeze_allowed:
        raise ValueError("Stage-1 readiness does not permit model freeze")
    if (
        readiness.state_hash != state.state_hash
        or readiness.protocol_hash != state.protocol_hash
        or readiness.snapshot_hash != state.snapshot_hash
    ):
        raise ValueError("readiness checkpoint does not match the exact training state")
    if state.cohort.model_frozen_at is not None:
        raise ValueError("prospective model is already frozen")
    if state.cohort.holdout_keys or state.cohort.post_holdout_keys:
        raise ValueError("model freeze cannot consume holdout evidence")

    artifact = ProspectiveModelFreezeArtifactV2(
        readiness_checkpoint_hash=readiness.checkpoint_hash,
        training_state_hash=state.state_hash,
        protocol_hash=state.protocol_hash,
        snapshot_hash=state.snapshot_hash,
        frozen_at=frozen_at,
        model_spec=model_spec,
        model_spec_hash=model_spec.model_spec_hash,
        simulation_seed=simulation_seed,
        acceptance_criteria=acceptance_criteria,
        acceptance_criteria_hash=acceptance_criteria.criteria_hash,
        tail_metrics=tail_metrics,
        source_normalization_rules=source_normalization_rules,
        missing_data_policy=missing_data_policy,
        fitting_rule=fitting_rule,
    )
    frozen_state = advance_prospective_evidence_state_v2(
        records=state.snapshot.records,
        protocol=state.protocol,
        model_frozen_at=frozen_at,
        previous_state=state,
    )
    return ProspectiveModelFreezeResultV2(
        artifact=artifact,
        frozen_state=frozen_state,
    )

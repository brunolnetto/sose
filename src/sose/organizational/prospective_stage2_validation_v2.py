from __future__ import annotations

from hashlib import sha256
import json
from math import ceil
import os
from pathlib import Path
import tempfile
from statistics import fmean, median
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

from .prospective_cohort_v2 import ProspectiveCohortStatus
from .prospective_model_freeze_v2 import (
    ProspectiveModelFreezeArtifactV2,
    REQUIRED_TAIL_METRICS_V2,
)
from .prospective_stage1_fit_v2 import (
    PRReviewV2Case,
    Stage1PRReviewFitV2,
    predict_pr_review_v2,
)
from .prospective_stage2_runner_v2 import (
    ProspectiveStage2CheckpointV2,
    build_stage2_checkpoint_v2,
)
from .prospective_state_v2 import ProspectiveEvidenceStateV2
from .source_evidence_v2 import GitHubPREvidenceSnapshotV2
from .validation import (
    LeadTimeValidationAssessment,
    assess_lead_time_validation,
    compare_lead_time_distributions,
)


STAGE2_VALIDATION_VERSION_V2 = "pr-review-prospective-stage2-validation/v2"
Sha256Hex = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]


class TailDistributionSummaryV2(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    count: int = Field(ge=1)
    mean: float = Field(ge=0.0, allow_inf_nan=False)
    median: float = Field(ge=0.0, allow_inf_nan=False)
    p90: float = Field(ge=0.0, allow_inf_nan=False)
    maximum: float = Field(ge=0.0, allow_inf_nan=False)
    max_to_median_ratio: float = Field(ge=1.0, allow_inf_nan=False)


class ProspectiveStage2ValidationArtifactV2(BaseModel):
    """Immutable result of the single untouched prospective holdout evaluation."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    validation_version: Literal[STAGE2_VALIDATION_VERSION_V2] = STAGE2_VALIDATION_VERSION_V2
    fit_hash: Sha256Hex
    freeze_hash: Sha256Hex
    checkpoint_hash: Sha256Hex
    state_hash: Sha256Hex
    model_spec_hash: Sha256Hex
    criteria_hash: Sha256Hex
    holdout_keys: tuple[tuple[str, int], ...]
    simulated_lead_times_seconds: dict[str, float]
    observed: TailDistributionSummaryV2
    simulated: TailDistributionSummaryV2
    ecdf_max_distance: float = Field(ge=0.0, le=1.0, allow_inf_nan=False)
    assessment: LeadTimeValidationAssessment
    reported_metrics: tuple[str, ...]
    passed: bool

    @model_validator(mode="after")
    def validate_derivations(self) -> "ProspectiveStage2ValidationArtifactV2":
        if self.reported_metrics != REQUIRED_TAIL_METRICS_V2:
            raise ValueError("reported metrics must exactly match prospective v2 requirements")
        if self.passed is not self.assessment.passed:
            raise ValueError("passed must equal the frozen assessment result")
        return self

    def canonical_payload(self) -> dict[str, object]:
        return {
            "validation_version": self.validation_version,
            "fit_hash": self.fit_hash,
            "freeze_hash": self.freeze_hash,
            "checkpoint_hash": self.checkpoint_hash,
            "state_hash": self.state_hash,
            "model_spec_hash": self.model_spec_hash,
            "criteria_hash": self.criteria_hash,
            "holdout_keys": [list(key) for key in self.holdout_keys],
            "simulated_lead_times_seconds": dict(sorted(self.simulated_lead_times_seconds.items())),
            "observed": self.observed.model_dump(mode="json"),
            "simulated": self.simulated.model_dump(mode="json"),
            "ecdf_max_distance": self.ecdf_max_distance,
            "assessment": self.assessment.model_dump(mode="json"),
            "reported_metrics": list(self.reported_metrics),
            "passed": self.passed,
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
    def validation_hash(self) -> str:
        return sha256(self.canonical_json().encode("utf-8")).hexdigest()


def validate_prospective_stage2_v2(
    *,
    state: ProspectiveEvidenceStateV2,
    checkpoint: ProspectiveStage2CheckpointV2,
    stage1_fit: Stage1PRReviewFitV2,
    model_freeze: ProspectiveModelFreezeArtifactV2,
) -> ProspectiveStage2ValidationArtifactV2:
    """Run exactly one validation after the preregistered 12/12 holdout is complete."""

    _validate_fit_freeze_binding(stage1_fit=stage1_fit, model_freeze=model_freeze)

    expected_checkpoint = build_stage2_checkpoint_v2(
        state=state,
        model_freeze=model_freeze,
    )
    if checkpoint.checkpoint_hash != expected_checkpoint.checkpoint_hash:
        raise ValueError("supplied checkpoint is not the exact Stage-2 checkpoint")
    if checkpoint.model_freeze_hash != model_freeze.freeze_hash:
        raise ValueError("supplied checkpoint is not bound to the exact frozen model")
    if (
        not checkpoint.complete
        or not checkpoint.validation_allowed
        or checkpoint.holdout_observed != checkpoint.holdout_target
        or checkpoint.holdout_target != 12
    ):
        raise ValueError("prospective Stage-2 validation requires exact 12/12 holdout completion")
    if state.cohort.status is not ProspectiveCohortStatus.COMPLETE:
        raise ValueError("prospective Stage-2 cohort must be complete")
    if state.cohort.post_holdout_keys:
        raise ValueError("prospective validation cannot consume post-holdout evidence")

    by_key = {
        (record.repository, record.pr_number): record
        for record in state.snapshot.records
    }
    holdout_records = tuple(by_key[key] for key in state.cohort.holdout_keys)
    holdout_snapshot = GitHubPREvidenceSnapshotV2(records=holdout_records)
    observed_dataset = holdout_snapshot.to_dataset()

    origin = min(record.opened_at for record in holdout_records)
    cases = tuple(
        PRReviewV2Case(
            pr_id=f"{record.repository}#{record.pr_number}",
            opened_at=(record.opened_at - origin).total_seconds(),
            author_is_bot=record.author_is_bot,
        )
        for record in holdout_records
    )
    prediction = predict_pr_review_v2(
        cases=cases,
        model=stage1_fit.model,
        seed=model_freeze.simulation_seed,
    )
    simulated = tuple(prediction.lead_time_seconds[case.pr_id] for case in cases)
    validation = compare_lead_time_distributions(
        observed=observed_dataset,
        simulated_seconds=simulated,
    )
    assessment = assess_lead_time_validation(
        validation=validation,
        criteria=model_freeze.acceptance_criteria,
    )
    observed_seconds = tuple(
        (record.merged_at - record.opened_at).total_seconds()
        for record in holdout_records
    )

    return ProspectiveStage2ValidationArtifactV2(
        fit_hash=stage1_fit.fit_hash,
        freeze_hash=model_freeze.freeze_hash,
        checkpoint_hash=checkpoint.checkpoint_hash,
        state_hash=state.state_hash,
        model_spec_hash=model_freeze.model_spec_hash,
        criteria_hash=model_freeze.acceptance_criteria_hash,
        holdout_keys=state.cohort.holdout_keys,
        simulated_lead_times_seconds=prediction.lead_time_seconds,
        observed=_tail_summary(observed_seconds),
        simulated=_tail_summary(simulated),
        ecdf_max_distance=validation.ecdf_max_distance,
        assessment=assessment,
        reported_metrics=REQUIRED_TAIL_METRICS_V2,
        passed=assessment.passed,
    )


def _validate_fit_freeze_binding(
    *,
    stage1_fit: Stage1PRReviewFitV2,
    model_freeze: ProspectiveModelFreezeArtifactV2,
) -> None:
    bindings = (
        (stage1_fit.training_state_hash, model_freeze.training_state_hash),
        (stage1_fit.protocol_hash, model_freeze.protocol_hash),
        (stage1_fit.snapshot_hash, model_freeze.snapshot_hash),
        (stage1_fit.model_spec_hash, model_freeze.model_spec_hash),
        (stage1_fit.simulation_seed, model_freeze.simulation_seed),
        (stage1_fit.acceptance_criteria_hash, model_freeze.acceptance_criteria_hash),
        (stage1_fit.tail_metrics, model_freeze.tail_metrics),
        (stage1_fit.source_normalization_rules, model_freeze.source_normalization_rules),
        (stage1_fit.missing_data_policy, model_freeze.missing_data_policy),
        (stage1_fit.fitting_rule, model_freeze.fitting_rule),
    )
    if any(left != right for left, right in bindings):
        raise ValueError("Stage-1 fit does not bind exactly to frozen model artifact")


def _tail_summary(values: tuple[float, ...]) -> TailDistributionSummaryV2:
    ordered = tuple(sorted(float(value) for value in values))
    if not ordered:
        raise ValueError("tail summary requires at least one value")
    med = median(ordered)
    rank = max(1, ceil(0.90 * len(ordered)))
    maximum = ordered[-1]
    if med == 0.0 and maximum > 0.0:
        raise ValueError("max-to-median ratio is undefined for zero median and positive maximum")
    ratio = maximum / med if med > 0.0 else 1.0
    return TailDistributionSummaryV2(
        count=len(ordered),
        mean=fmean(ordered),
        median=med,
        p90=ordered[rank - 1],
        maximum=maximum,
        max_to_median_ratio=ratio,
    )


def run_prospective_stage2_validation_files_v2(
    *,
    stage1_fit_path: str | Path,
    model_freeze_path: str | Path,
    state_path: str | Path,
    checkpoint_path: str | Path,
    output_path: str | Path,
) -> ProspectiveStage2ValidationArtifactV2:
    """Execute the frozen validation from immutable local artifacts only."""

    fit = Stage1PRReviewFitV2.model_validate_json(
        Path(stage1_fit_path).read_text(encoding="utf-8")
    )
    freeze = ProspectiveModelFreezeArtifactV2.model_validate_json(
        Path(model_freeze_path).read_text(encoding="utf-8")
    )
    state = ProspectiveEvidenceStateV2.model_validate_json(
        Path(state_path).read_text(encoding="utf-8")
    )
    checkpoint = ProspectiveStage2CheckpointV2.model_validate_json(
        Path(checkpoint_path).read_text(encoding="utf-8")
    )
    result = validate_prospective_stage2_v2(
        state=state,
        checkpoint=checkpoint,
        stage1_fit=fit,
        model_freeze=freeze,
    )
    _publish_exclusive(Path(output_path), result.canonical_json() + "\n")
    return result


def _publish_exclusive(path: Path, serialized: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        if path.read_text(encoding="utf-8") == serialized:
            return
        raise FileExistsError(f"output already contains a different artifact: {path}")

    descriptor, temporary_name = tempfile.mkstemp(
        dir=path.parent,
        prefix=f".{path.name}.",
        suffix=".tmp",
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(serialized)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.link(temporary, path)
        except FileExistsError:
            if path.read_text(encoding="utf-8") != serialized:
                raise
    finally:
        temporary.unlink(missing_ok=True)

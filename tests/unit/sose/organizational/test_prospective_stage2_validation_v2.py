from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from sose.organizational.prospective_model_freeze_v2 import (
    ProspectiveModelFreezeArtifactV2,
    REQUIRED_TAIL_METRICS_V2,
)
from sose.organizational.prospective_protocol_v2 import ProspectiveStudyProtocolV2
from sose.organizational.prospective_stage1_fit_v2 import (
    DelayAnalogV2,
    PRReviewV2TailModel,
    Stage1PRReviewFitV2,
)
from sose.organizational.prospective_stage2_runner_v2 import build_stage2_checkpoint_v2
from sose.organizational.prospective_stage2_validation_v2 import (
    run_prospective_stage2_validation_files_v2,
    validate_prospective_stage2_v2,
)
from sose.organizational.prospective_state_v2 import advance_prospective_evidence_state_v2
from sose.organizational.source_evidence_v2 import GitHubPREvidenceRecordV2
from sose.organizational.validation import LeadTimeValidationCriteria


REGISTERED_AT = datetime(2026, 10, 6, 14, 44, 13, tzinfo=UTC)
FROZEN_AT = datetime(2026, 10, 7, 0, 21, 36, tzinfo=UTC)


def test_validation_rejects_incomplete_holdout() -> None:
    fit, freeze, frozen = _frozen()
    state = _with_holdout(frozen, count=11)
    checkpoint = build_stage2_checkpoint_v2(state=state, model_freeze=freeze)

    with pytest.raises(ValueError, match="12/12"):
        validate_prospective_stage2_v2(
            state=state,
            checkpoint=checkpoint,
            stage1_fit=fit,
            model_freeze=freeze,
        )


def test_validation_binds_exact_fit_freeze_and_reports_all_tail_metrics() -> None:
    fit, freeze, frozen = _frozen()
    state = _with_holdout(frozen, count=12)
    checkpoint = build_stage2_checkpoint_v2(state=state, model_freeze=freeze)

    result = validate_prospective_stage2_v2(
        state=state,
        checkpoint=checkpoint,
        stage1_fit=fit,
        model_freeze=freeze,
    )

    assert result.fit_hash == fit.fit_hash
    assert result.freeze_hash == freeze.freeze_hash
    assert result.checkpoint_hash == checkpoint.checkpoint_hash
    assert result.state_hash == state.state_hash
    assert result.holdout_keys == state.cohort.holdout_keys
    assert result.reported_metrics == REQUIRED_TAIL_METRICS_V2
    assert result.observed.maximum >= result.observed.p90
    assert result.simulated.maximum >= result.simulated.p90
    assert result.observed.max_to_median_ratio >= 1.0
    assert result.simulated.max_to_median_ratio >= 1.0
    assert len(result.assessment.checks) == 4
    assert result.passed == result.assessment.passed


def test_validation_rejects_fit_not_bound_to_freeze() -> None:
    fit, freeze, frozen = _frozen()
    state = _with_holdout(frozen, count=12)
    checkpoint = build_stage2_checkpoint_v2(state=state, model_freeze=freeze)
    forged = fit.model_copy(update={"simulation_seed": fit.simulation_seed + 1})

    with pytest.raises(ValueError, match="Stage-1 fit does not bind"):
        validate_prospective_stage2_v2(
            state=state,
            checkpoint=checkpoint,
            stage1_fit=forged,
            model_freeze=freeze,
        )


def test_validation_rejects_tampered_fit_model_even_if_stored_spec_hash_is_unchanged() -> None:
    fit, freeze, frozen = _frozen()
    state = _with_holdout(frozen, count=12)
    checkpoint = build_stage2_checkpoint_v2(state=state, model_freeze=freeze)
    tampered_model = fit.model.model_copy(
        update={
            "human_analogs": (
                DelayAnalogV2(
                    workflow_active_seconds=9_999.0,
                    unidentified_residual_seconds=9_999.0,
                ),
            )
        }
    )
    forged = fit.model_copy(update={"model": tampered_model})

    with pytest.raises(ValueError, match="fit model does not reconstruct"):
        validate_prospective_stage2_v2(
            state=state,
            checkpoint=checkpoint,
            stage1_fit=forged,
            model_freeze=freeze,
        )


def test_predictions_do_not_depend_on_holdout_terminal_outcomes() -> None:
    fit, freeze, frozen = _frozen()
    first = _with_holdout(frozen, count=12, extra_seconds=0)
    second = _with_holdout(frozen, count=12, extra_seconds=10_000)

    first_result = validate_prospective_stage2_v2(
        state=first,
        checkpoint=build_stage2_checkpoint_v2(state=first, model_freeze=freeze),
        stage1_fit=fit,
        model_freeze=freeze,
    )
    second_result = validate_prospective_stage2_v2(
        state=second,
        checkpoint=build_stage2_checkpoint_v2(state=second, model_freeze=freeze),
        stage1_fit=fit,
        model_freeze=freeze,
    )

    assert first_result.simulated_lead_times_seconds == second_result.simulated_lead_times_seconds
    assert first_result.observed != second_result.observed


def test_file_runner_publishes_one_idempotent_validation_artifact(tmp_path: Path) -> None:
    fit, freeze, frozen = _frozen()
    state = _with_holdout(frozen, count=12)
    checkpoint = build_stage2_checkpoint_v2(state=state, model_freeze=freeze)

    fit_path = tmp_path / "fit.json"
    freeze_path = tmp_path / "freeze.json"
    state_path = tmp_path / "state.json"
    checkpoint_path = tmp_path / "checkpoint.json"
    output_path = tmp_path / "validation.json"

    fit_path.write_text(fit.canonical_json() + "\n", encoding="utf-8")
    freeze_path.write_text(freeze.canonical_json() + "\n", encoding="utf-8")
    state_path.write_text(state.canonical_json() + "\n", encoding="utf-8")
    checkpoint_path.write_text(checkpoint.canonical_json() + "\n", encoding="utf-8")

    first = run_prospective_stage2_validation_files_v2(
        stage1_fit_path=fit_path,
        model_freeze_path=freeze_path,
        state_path=state_path,
        checkpoint_path=checkpoint_path,
        output_path=output_path,
    )
    repeated = run_prospective_stage2_validation_files_v2(
        stage1_fit_path=fit_path,
        model_freeze_path=freeze_path,
        state_path=state_path,
        checkpoint_path=checkpoint_path,
        output_path=output_path,
    )

    assert repeated == first
    assert output_path.read_text(encoding="utf-8") == first.canonical_json() + "\n"


def _protocol() -> ProspectiveStudyProtocolV2:
    return ProspectiveStudyProtocolV2(
        protocol_document_hash="a" * 64,
        registration_merged_at=REGISTERED_AT,
    )


def _training_records() -> tuple[GitHubPREvidenceRecordV2, ...]:
    return tuple(
        _record(
            302 + index,
            opened_at=REGISTERED_AT + timedelta(minutes=index + 1),
            lead_seconds=60 + index,
            author_is_bot=index in {16, 17},
        )
        for index in range(18)
    )


def _frozen():
    training = _training_records()
    pre = advance_prospective_evidence_state_v2(records=training, protocol=_protocol())

    model = PRReviewV2TailModel(
        source_snapshot_hash=pre.snapshot_hash,
        human_analogs=(
            DelayAnalogV2(workflow_active_seconds=100.0, unidentified_residual_seconds=200.0),
            DelayAnalogV2(workflow_active_seconds=200.0, unidentified_residual_seconds=800.0),
        ),
        bot_analogs=(
            DelayAnalogV2(workflow_active_seconds=100.0, unidentified_residual_seconds=2_000.0),
        ),
    )
    spec = model.build_model_spec()
    criteria = LeadTimeValidationCriteria(
        max_abs_mean_difference_seconds=10_000.0,
        max_abs_median_difference_seconds=10_000.0,
        max_abs_p90_difference_seconds=10_000.0,
        max_ecdf_distance=1.0,
    )
    fit = Stage1PRReviewFitV2(
        training_state_hash=pre.state_hash,
        protocol_hash=pre.protocol_hash,
        snapshot_hash=pre.snapshot_hash,
        training_keys=pre.cohort.training_keys,
        model=model,
        model_spec=spec.canonical_payload(),
        model_spec_hash=spec.model_spec_hash,
        simulation_seed=20261005,
        training_bot_fraction=2 / 18,
        acceptance_bootstrap_seed=20261006,
        acceptance_bootstrap_replicates=4096,
        acceptance_quantile=0.95,
        acceptance_criteria=criteria,
        acceptance_criteria_hash=criteria.criteria_hash,
        tail_metrics=REQUIRED_TAIL_METRICS_V2,
        source_normalization_rules="frozen",
        missing_data_policy="frozen",
        fitting_rule="frozen",
    )
    freeze = ProspectiveModelFreezeArtifactV2(
        readiness_checkpoint_hash="1" * 64,
        training_state_hash=fit.training_state_hash,
        protocol_hash=fit.protocol_hash,
        snapshot_hash=fit.snapshot_hash,
        frozen_at=FROZEN_AT,
        model_spec=fit.model_spec.canonical_payload(),
        model_spec_hash=fit.model_spec_hash,
        simulation_seed=fit.simulation_seed,
        acceptance_criteria=fit.acceptance_criteria,
        acceptance_criteria_hash=fit.acceptance_criteria_hash,
        tail_metrics=REQUIRED_TAIL_METRICS_V2,
        source_normalization_rules=fit.source_normalization_rules,
        missing_data_policy=fit.missing_data_policy,
        fitting_rule=fit.fitting_rule,
    )
    frozen = advance_prospective_evidence_state_v2(
        records=training,
        protocol=_protocol(),
        model_frozen_at=FROZEN_AT,
        previous_state=pre,
    )
    return fit, freeze, frozen


def _with_holdout(frozen, *, count: int, extra_seconds: int = 0):
    holdout = tuple(
        _record(
            400 + index,
            opened_at=FROZEN_AT + timedelta(minutes=index + 1),
            lead_seconds=300 + index * 20 + extra_seconds,
            author_is_bot=index % 7 == 0,
        )
        for index in range(count)
    )
    return advance_prospective_evidence_state_v2(
        records=(*frozen.snapshot.records, *holdout),
        protocol=frozen.protocol,
        model_frozen_at=FROZEN_AT,
        previous_state=frozen,
    )


def _record(
    pr_number: int,
    *,
    opened_at: datetime,
    lead_seconds: int,
    author_is_bot: bool = False,
) -> GitHubPREvidenceRecordV2:
    return GitHubPREvidenceRecordV2(
        repository="brunolnetto/sose",
        pr_number=pr_number,
        opened_at=opened_at,
        merged_at=opened_at + timedelta(seconds=lead_seconds),
        source_url=f"https://api.github.com/repos/brunolnetto/sose/pulls/{pr_number}",
        author_actor_key=("renovate[bot]" if author_is_bot else "brunolnetto"),
        author_is_bot=author_is_bot,
    )

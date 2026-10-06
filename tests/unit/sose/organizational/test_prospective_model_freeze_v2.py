from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from sose.examples.organizational_pr_review import PullRequestFlowConfig, build_model_spec
from sose.organizational.prospective_model_freeze_v2 import (
    REQUIRED_TAIL_METRICS_V2,
    freeze_prospective_model_v2,
)
from sose.organizational.prospective_protocol_v2 import ProspectiveStudyProtocolV2
from sose.organizational.prospective_stage1_v2 import build_stage1_readiness_checkpoint_v2
from sose.organizational.prospective_state_v2 import advance_prospective_evidence_state_v2
from sose.organizational.source_evidence_v2 import GitHubPREvidenceRecordV2
from sose.organizational.validation import LeadTimeValidationCriteria


REGISTERED_AT = datetime(2026, 10, 6, 15, 0, tzinfo=UTC)


def test_freeze_rejects_incomplete_stage1() -> None:
    state = _state(17)
    checkpoint = build_stage1_readiness_checkpoint_v2(state)

    with pytest.raises(ValueError, match="Stage-1 readiness"):
        _freeze(state, checkpoint, REGISTERED_AT + timedelta(hours=2))


def test_freeze_binds_all_preregistered_model_decisions_and_advances_state() -> None:
    state = _state(18)
    checkpoint = build_stage1_readiness_checkpoint_v2(state)
    frozen_at = max(record.merged_at for record in state.snapshot.records) + timedelta(minutes=1)

    result = _freeze(state, checkpoint, frozen_at)

    artifact = result.artifact
    assert artifact.readiness_checkpoint_hash == checkpoint.checkpoint_hash
    assert artifact.training_state_hash == state.state_hash
    assert artifact.protocol_hash == state.protocol_hash
    assert artifact.snapshot_hash == state.snapshot_hash
    assert artifact.model_spec_hash == artifact.model_spec.model_spec_hash
    assert artifact.acceptance_criteria_hash == artifact.acceptance_criteria.criteria_hash
    assert artifact.tail_metrics == REQUIRED_TAIL_METRICS_V2
    assert artifact.simulation_seed == 20261006
    assert artifact.source_normalization_rules
    assert artifact.missing_data_policy
    assert artifact.fitting_rule
    assert len(artifact.freeze_hash) == 64

    assert result.frozen_state.previous_state_hash == state.state_hash
    assert result.frozen_state.cohort.model_frozen_at == frozen_at
    assert result.frozen_state.cohort.status.value == "collecting_holdout"
    assert result.frozen_state.cohort.holdout_keys == ()


def test_freeze_requires_checkpoint_to_match_exact_training_state() -> None:
    state = _state(18)
    other = _state(18, protocol_hash_seed="1")
    checkpoint = build_stage1_readiness_checkpoint_v2(other)
    frozen_at = max(record.merged_at for record in state.snapshot.records) + timedelta(minutes=1)

    with pytest.raises(ValueError, match="checkpoint"):
        _freeze(state, checkpoint, frozen_at)



def test_freeze_rejects_forged_readiness_details_even_when_state_hashes_match() -> None:
    state = _state(18)
    checkpoint = build_stage1_readiness_checkpoint_v2(state)
    forged = checkpoint.model_copy(update={"interstitial_count": 1})
    frozen_at = max(record.merged_at for record in state.snapshot.records) + timedelta(minutes=1)

    with pytest.raises(ValueError, match="exact readiness checkpoint"):
        _freeze(state, forged, frozen_at)


def test_freeze_rejects_backdating_before_any_collected_evidence_was_available() -> None:
    state = _state(19)
    checkpoint = build_stage1_readiness_checkpoint_v2(state)
    training_completed_at = max(record.merged_at for record in state.snapshot.records[:18])
    frozen_at = training_completed_at + timedelta(seconds=30)

    assert state.snapshot.records[18].merged_at > frozen_at
    with pytest.raises(ValueError, match="collected evidence"):
        _freeze(state, checkpoint, frozen_at)

def test_freeze_rejects_missing_required_tail_metric() -> None:
    state = _state(18)
    checkpoint = build_stage1_readiness_checkpoint_v2(state)
    frozen_at = max(record.merged_at for record in state.snapshot.records) + timedelta(minutes=1)

    with pytest.raises(ValueError, match="tail metrics"):
        _freeze(
            state,
            checkpoint,
            frozen_at,
            tail_metrics=REQUIRED_TAIL_METRICS_V2[:-1],
        )


def _freeze(state, checkpoint, frozen_at, **overrides):
    payload = {
        "state": state,
        "readiness": checkpoint,
        "frozen_at": frozen_at,
        "model_spec": build_model_spec(PullRequestFlowConfig()),
        "simulation_seed": 20261006,
        "acceptance_criteria": LeadTimeValidationCriteria(
            max_abs_mean_difference_seconds=1800.0,
            max_abs_median_difference_seconds=900.0,
            max_abs_p90_difference_seconds=3600.0,
            max_ecdf_distance=0.25,
        ),
        "tail_metrics": REQUIRED_TAIL_METRICS_V2,
        "source_normalization_rules": "Use the frozen v2 GitHub source adapter; infer no actor effort from timestamp gaps.",
        "missing_data_policy": "Retain eligible PRs; represent unavailable mechanisms as Assumed/Inferable rather than imputing actor time.",
        "fitting_rule": "Fit only from the 18 Stage-1 training PRs; no Stage-2 outcomes may alter parameters.",
    }
    payload.update(overrides)
    return freeze_prospective_model_v2(**payload)


def _state(count: int, *, protocol_hash_seed: str = "0"):
    return advance_prospective_evidence_state_v2(
        records=_records(count),
        protocol=ProspectiveStudyProtocolV2(
            protocol_document_hash=protocol_hash_seed * 64,
            registration_merged_at=REGISTERED_AT,
        ),
    )


def _records(count: int) -> tuple[GitHubPREvidenceRecordV2, ...]:
    return tuple(
        GitHubPREvidenceRecordV2(
            repository="brunolnetto/sose",
            pr_number=302 + index,
            opened_at=REGISTERED_AT + timedelta(minutes=index + 1),
            merged_at=REGISTERED_AT + timedelta(minutes=index + 2),
            source_url=f"https://api.github.com/repos/brunolnetto/sose/pulls/{302 + index}",
        )
        for index in range(count)
    )

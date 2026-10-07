from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from sose.organizational.acquisition_v2 import (
    GitHubEndpointCaptureV2,
    GitHubPRAcquisitionArtifactV2,
    GitHubPRAcquisitionManifestV2,
    GitHubPRAcquisitionSnapshotV2,
)
from sose.organizational.prospective_model_freeze_v2 import (
    ProspectiveModelFreezeArtifactV2,
    REQUIRED_TAIL_METRICS_V2,
)
from sose.organizational.prospective_protocol_v2 import ProspectiveStudyProtocolV2
from sose.organizational.prospective_stage2_runner_v2 import (
    ProspectiveStage2CheckpointV2,
    advance_stage2_artifacts_v2,
)
from sose.organizational.prospective_state_v2 import advance_prospective_evidence_state_v2
from sose.organizational.source_evidence_v2 import GitHubPREvidenceRecordV2
from sose.organizational.validation import LeadTimeValidationCriteria
from sose.examples.organizational_pr_review import PullRequestFlowConfig, build_model_spec


REGISTERED_AT = datetime(2026, 10, 6, 14, 44, 13, tzinfo=UTC)
FROZEN_AT = datetime(2026, 10, 7, 0, 21, 36, tzinfo=UTC)


def test_stage2_checkpoint_reports_progress_without_permitting_early_validation() -> None:
    previous = _frozen_state()
    result = advance_stage2_artifacts_v2(
        model_freeze=_freeze(previous),
        tranche=_snapshot(
            _record(330, opened_at=FROZEN_AT + timedelta(minutes=1)),
            _record(331, opened_at=FROZEN_AT + timedelta(minutes=2)),
        ),
        previous_acquisition=_snapshot(*previous.snapshot.records),
        previous_state=previous,
    )

    assert result.state.cohort.holdout_keys == (
        ("brunolnetto/sose", 330),
        ("brunolnetto/sose", 331),
    )
    assert result.checkpoint.holdout_observed == 2
    assert result.checkpoint.holdout_target == 12
    assert result.checkpoint.holdout_remaining == 10
    assert result.checkpoint.validation_allowed is False
    assert result.checkpoint.complete is False


def test_stage2_checkpoint_allows_validation_only_at_exact_holdout_target() -> None:
    previous = _frozen_state()
    records = tuple(
        _record(330 + index, opened_at=FROZEN_AT + timedelta(minutes=index + 1))
        for index in range(12)
    )
    result = advance_stage2_artifacts_v2(
        model_freeze=_freeze(previous),
        tranche=_snapshot(*records),
        previous_acquisition=_snapshot(*previous.snapshot.records),
        previous_state=previous,
    )

    assert len(result.state.cohort.holdout_keys) == 12
    assert result.state.cohort.post_holdout_keys == ()
    assert result.checkpoint.holdout_observed == 12
    assert result.checkpoint.holdout_remaining == 0
    assert result.checkpoint.validation_allowed is True
    assert result.checkpoint.complete is True


def test_stage2_keeps_pre_freeze_late_merges_interstitial() -> None:
    previous = _frozen_state()
    late_interstitial = _record(
        329,
        opened_at=FROZEN_AT - timedelta(seconds=30),
        merged_at=FROZEN_AT + timedelta(minutes=5),
    )
    holdout = _record(
        330,
        opened_at=FROZEN_AT + timedelta(seconds=1),
        merged_at=FROZEN_AT + timedelta(minutes=6),
    )

    result = advance_stage2_artifacts_v2(
        model_freeze=_freeze(previous),
        tranche=_snapshot(late_interstitial, holdout),
        previous_acquisition=_snapshot(*previous.snapshot.records),
        previous_state=previous,
    )

    assert ("brunolnetto/sose", 329) in result.state.cohort.interstitial_keys
    assert result.state.cohort.holdout_keys == (("brunolnetto/sose", 330),)


def test_stage2_rejects_freeze_artifact_not_bound_to_previous_frozen_state() -> None:
    previous = _frozen_state()
    freeze = _freeze(previous)
    forged = freeze.model_copy(update={"protocol_hash": "f" * 64})

    with pytest.raises(ValueError, match="freeze artifact does not bind"):
        advance_stage2_artifacts_v2(
            model_freeze=forged,
            tranche=_snapshot(_record(330, opened_at=FROZEN_AT + timedelta(minutes=1))),
            previous_acquisition=_snapshot(*previous.snapshot.records),
            previous_state=previous,
        )



def test_stage2_accepts_freeze_snapshot_that_contains_pre_freeze_interstitial() -> None:
    protocol = _protocol()
    training = (
        _record(302, opened_at=REGISTERED_AT + timedelta(minutes=1)),
        _record(303, opened_at=REGISTERED_AT + timedelta(minutes=2)),
    )
    pre = advance_prospective_evidence_state_v2(records=training, protocol=protocol)
    with_interstitial = advance_prospective_evidence_state_v2(
        records=(
            *training,
            _record(
                329,
                opened_at=FROZEN_AT - timedelta(minutes=1),
                merged_at=FROZEN_AT - timedelta(seconds=1),
            ),
        ),
        protocol=protocol,
        previous_state=pre,
    )
    frozen = advance_prospective_evidence_state_v2(
        records=with_interstitial.snapshot.records,
        protocol=protocol,
        model_frozen_at=FROZEN_AT,
        previous_state=with_interstitial,
    )
    freeze = _freeze_for_state(
        frozen,
        training_state_hash=with_interstitial.state_hash,
        snapshot_hash=with_interstitial.snapshot_hash,
    )

    result = advance_stage2_artifacts_v2(
        model_freeze=freeze,
        tranche=_snapshot(_record(330, opened_at=FROZEN_AT + timedelta(minutes=1))),
        previous_acquisition=_snapshot(*frozen.snapshot.records),
        previous_state=frozen,
    )

    assert ("brunolnetto/sose", 329) in result.state.cohort.interstitial_keys
    assert result.state.cohort.holdout_keys == (("brunolnetto/sose", 330),)


def test_stage2_rejects_different_freeze_identity_with_same_protocol_and_snapshot() -> None:
    previous = _frozen_state()
    freeze = _freeze(previous)
    forged = freeze.model_copy(update={"training_state_hash": "e" * 64})

    with pytest.raises(ValueError, match="exact pre-freeze state"):
        advance_stage2_artifacts_v2(
            model_freeze=forged,
            tranche=_snapshot(_record(330, opened_at=FROZEN_AT + timedelta(minutes=1))),
            previous_acquisition=_snapshot(*previous.snapshot.records),
            previous_state=previous,
        )


def test_later_stage2_tranche_requires_previous_checkpoint_chain() -> None:
    previous = _frozen_state()
    freeze = _freeze(previous)
    first = advance_stage2_artifacts_v2(
        model_freeze=freeze,
        tranche=_snapshot(_record(330, opened_at=FROZEN_AT + timedelta(minutes=1))),
        previous_acquisition=_snapshot(*previous.snapshot.records),
        previous_state=previous,
    )

    with pytest.raises(ValueError, match="previous Stage-2 checkpoint"):
        advance_stage2_artifacts_v2(
            model_freeze=freeze,
            tranche=_snapshot(_record(331, opened_at=FROZEN_AT + timedelta(minutes=2))),
            previous_acquisition=first.acquisition,
            previous_state=first.state,
        )

    second = advance_stage2_artifacts_v2(
        model_freeze=freeze,
        tranche=_snapshot(_record(331, opened_at=FROZEN_AT + timedelta(minutes=2))),
        previous_acquisition=first.acquisition,
        previous_state=first.state,
        previous_checkpoint=first.checkpoint,
    )
    assert second.checkpoint.holdout_observed == 2
    assert second.checkpoint.model_freeze_hash == first.checkpoint.model_freeze_hash


def test_checkpoint_rejects_forged_progress_fields() -> None:
    previous = _frozen_state()
    result = advance_stage2_artifacts_v2(
        model_freeze=_freeze(previous),
        tranche=_snapshot(_record(330, opened_at=FROZEN_AT + timedelta(minutes=1))),
        previous_acquisition=_snapshot(*previous.snapshot.records),
        previous_state=previous,
    )
    payload = result.checkpoint.canonical_payload()
    payload.update(
        holdout_observed=12,
        holdout_remaining=0,
        validation_allowed=True,
        complete=True,
    )

    with pytest.raises(ValueError, match="holdout_observed must match holdout_keys"):
        ProspectiveStage2CheckpointV2.model_validate(payload)


def _protocol() -> ProspectiveStudyProtocolV2:
    return ProspectiveStudyProtocolV2(
        protocol_document_hash="0" * 64,
        registration_merged_at=REGISTERED_AT,
        training_count=2,
        holdout_count=12,
    )


def _frozen_state():
    training = (
        _record(302, opened_at=REGISTERED_AT + timedelta(minutes=1)),
        _record(303, opened_at=REGISTERED_AT + timedelta(minutes=2)),
    )
    pre = advance_prospective_evidence_state_v2(records=training, protocol=_protocol())
    return advance_prospective_evidence_state_v2(
        records=training,
        protocol=_protocol(),
        model_frozen_at=FROZEN_AT,
        previous_state=pre,
    )


def _freeze(frozen_state) -> ProspectiveModelFreezeArtifactV2:
    return _freeze_for_state(
        frozen_state,
        training_state_hash=frozen_state.previous_state_hash,
        snapshot_hash=frozen_state.snapshot_hash,
    )


def _freeze_for_state(
    frozen_state,
    *,
    training_state_hash: str,
    snapshot_hash: str,
) -> ProspectiveModelFreezeArtifactV2:
    spec = build_model_spec(PullRequestFlowConfig())
    criteria = LeadTimeValidationCriteria(
        max_abs_mean_difference_seconds=1.0,
        max_abs_median_difference_seconds=1.0,
        max_abs_p90_difference_seconds=1.0,
        max_ecdf_distance=0.5,
    )
    return ProspectiveModelFreezeArtifactV2(
        readiness_checkpoint_hash="1" * 64,
        training_state_hash=training_state_hash,
        protocol_hash=frozen_state.protocol_hash,
        snapshot_hash=snapshot_hash,
        frozen_at=FROZEN_AT,
        model_spec=spec,
        model_spec_hash=spec.model_spec_hash,
        simulation_seed=7,
        acceptance_criteria=criteria,
        acceptance_criteria_hash=criteria.criteria_hash,
        tail_metrics=REQUIRED_TAIL_METRICS_V2,
        source_normalization_rules="frozen",
        missing_data_policy="frozen",
        fitting_rule="frozen",
    )


def _record(
    pr_number: int,
    *,
    opened_at: datetime,
    merged_at: datetime | None = None,
) -> GitHubPREvidenceRecordV2:
    return GitHubPREvidenceRecordV2(
        repository="brunolnetto/sose",
        pr_number=pr_number,
        opened_at=opened_at,
        merged_at=merged_at or opened_at + timedelta(seconds=30),
        source_url=f"https://api.github.com/repos/brunolnetto/sose/pulls/{pr_number}",
        author_actor_key="brunolnetto",
    )


def _snapshot(*records: GitHubPREvidenceRecordV2) -> GitHubPRAcquisitionSnapshotV2:
    def capture(endpoint: str, record: GitHubPREvidenceRecordV2) -> GitHubEndpointCaptureV2:
        return GitHubEndpointCaptureV2(
            endpoint=endpoint,
            pages_fetched=1,
            record_count=(1 if endpoint == "pull_request" else 0),
            complete=True,
            source_urls=(f"{record.source_url}/{endpoint}?page=1",),
        )

    artifacts = []
    for record in records:
        manifest = GitHubPRAcquisitionManifestV2(
            repository=record.repository,
            pr_number=record.pr_number,
            captured_at=record.merged_at + timedelta(seconds=1),
            pull_request=capture("pull_request", record),
            timeline=capture("timeline", record),
            reviews=capture("reviews", record),
            workflow_runs=capture("workflow_runs", record),
            workflow_jobs=GitHubEndpointCaptureV2(
                endpoint="workflow_jobs",
                pages_fetched=0,
                record_count=0,
                complete=True,
            ),
        )
        artifacts.append(GitHubPRAcquisitionArtifactV2(manifest=manifest, evidence=record))
    return GitHubPRAcquisitionSnapshotV2(artifacts=tuple(artifacts))

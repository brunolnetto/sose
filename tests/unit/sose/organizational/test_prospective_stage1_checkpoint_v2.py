from __future__ import annotations

from datetime import UTC, datetime, timedelta
import json

import pytest
from pydantic import ValidationError

from sose.organizational.prospective_protocol_v2 import ProspectiveStudyProtocolV2
from sose.organizational.prospective_stage1_v2 import (
    STAGE1_CHECKPOINT_VERSION,
    ProspectiveStage1ReadinessCheckpointV2,
    build_stage1_readiness_checkpoint_v2,
    publish_stage1_readiness_checkpoint_v2,
)
from sose.organizational.prospective_state_v2 import advance_prospective_evidence_state_v2
from sose.organizational.source_evidence_v2 import GitHubPREvidenceRecordV2


REGISTERED_AT = datetime(2026, 10, 6, 15, 0, tzinfo=UTC)


def test_partial_training_checkpoint_is_hash_bound_and_freeze_ineligible() -> None:
    state = _state(8)

    checkpoint = build_stage1_readiness_checkpoint_v2(state)

    assert checkpoint.checkpoint_version == STAGE1_CHECKPOINT_VERSION
    assert checkpoint.state_hash == state.state_hash
    assert checkpoint.protocol_hash == state.protocol_hash
    assert checkpoint.snapshot_hash == state.snapshot_hash
    assert checkpoint.training_observed == 8
    assert checkpoint.training_target == 18
    assert checkpoint.training_remaining == 10
    assert checkpoint.freeze_allowed is False
    assert checkpoint.training_keys == tuple(
        ("brunolnetto/sose", number) for number in range(302, 310)
    )
    assert checkpoint.holdout_exposed is False
    assert len(checkpoint.checkpoint_hash) == 64


def test_complete_training_checkpoint_allows_freeze_but_exposes_no_holdout() -> None:
    state = _state(18)

    checkpoint = build_stage1_readiness_checkpoint_v2(state)

    assert checkpoint.training_observed == 18
    assert checkpoint.training_remaining == 0
    assert checkpoint.freeze_allowed is True
    assert checkpoint.holdout_exposed is False
    assert checkpoint.interstitial_count == 0


def test_stage1_checkpoint_rejects_state_after_model_freeze() -> None:
    records = _records(19)
    training_completed = max(record.merged_at for record in records[:18])
    frozen_at = training_completed + timedelta(minutes=1)
    after_freeze = GitHubPREvidenceRecordV2(
        repository="brunolnetto/sose",
        pr_number=400,
        opened_at=frozen_at + timedelta(minutes=1),
        merged_at=frozen_at + timedelta(minutes=2),
        source_url="https://api.github.com/repos/brunolnetto/sose/pulls/400",
    )
    state = advance_prospective_evidence_state_v2(
        records=(*records[:18], after_freeze),
        protocol=_protocol(),
        model_frozen_at=frozen_at,
    )

    with pytest.raises(ValueError, match="before model freeze"):
        build_stage1_readiness_checkpoint_v2(state)


def test_checkpoint_canonical_json_and_hash_are_deterministic() -> None:
    state = _state(8)

    first = build_stage1_readiness_checkpoint_v2(state)
    second = build_stage1_readiness_checkpoint_v2(state)

    assert first.canonical_json() == second.canonical_json()
    assert first.checkpoint_hash == second.checkpoint_hash
    payload = json.loads(first.canonical_json())
    assert payload["state_hash"] == state.state_hash
    assert payload["training_remaining"] == 10


def test_checkpoint_rejects_forged_derived_readiness_fields() -> None:
    checkpoint = build_stage1_readiness_checkpoint_v2(_state(8))
    payload = checkpoint.canonical_payload()
    payload.update(
        training_observed=18,
        training_remaining=0,
        freeze_allowed=True,
    )

    with pytest.raises(ValidationError, match="training_observed must match training_keys"):
        ProspectiveStage1ReadinessCheckpointV2.model_validate(payload)


def test_checkpoint_rejects_duplicate_training_keys() -> None:
    checkpoint = build_stage1_readiness_checkpoint_v2(_state(8))
    payload = checkpoint.canonical_payload()
    payload["training_keys"] = [payload["training_keys"][0]] * 8

    with pytest.raises(ValidationError, match="training_keys must be unique"):
        ProspectiveStage1ReadinessCheckpointV2.model_validate(payload)


def test_publish_is_idempotent_and_refuses_replacement(tmp_path) -> None:
    state = _state(8)
    output = tmp_path / "stage1-checkpoint.json"

    first = publish_stage1_readiness_checkpoint_v2(state=state, output_path=output)
    second = publish_stage1_readiness_checkpoint_v2(state=state, output_path=output)

    assert first == second
    assert output.read_text(encoding="utf-8") == first.canonical_json() + "\n"

    different = _state(9)
    with pytest.raises(FileExistsError, match="different artifact"):
        publish_stage1_readiness_checkpoint_v2(state=different, output_path=output)


def _state(count: int):
    return advance_prospective_evidence_state_v2(
        records=_records(count),
        protocol=_protocol(),
    )


def _protocol() -> ProspectiveStudyProtocolV2:
    return ProspectiveStudyProtocolV2(
        protocol_document_hash="0" * 64,
        registration_merged_at=REGISTERED_AT,
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

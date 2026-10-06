from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from sose.organizational.prospective_cohort_v2 import ProspectiveCohortStatus
from sose.organizational.prospective_protocol_v2 import ProspectiveStudyProtocolV2
from sose.organizational.prospective_state_v2 import (
    ProspectiveEvidenceStateV2,
    advance_prospective_evidence_state_v2,
)
from sose.organizational.source_evidence_v2 import (
    GitHubPREvidenceRecordV2,
    GitHubReviewSourceRecord,
)


REGISTERED_AT = datetime(2026, 10, 6, 14, 44, 13, tzinfo=UTC)
REPOSITORY = "brunolnetto/sose"
PROTOCOL = ProspectiveStudyProtocolV2(
    protocol_document_hash="a" * 64,
    registration_merged_at=REGISTERED_AT,
)


def test_initial_state_binds_canonical_source_snapshot_to_enrollment() -> None:
    second = _record(303, minute=10)
    first = _record(302, minute=5)

    state = advance_prospective_evidence_state_v2(records=(second, first), protocol=PROTOCOL)

    assert state.previous_state_hash is None
    assert state.protocol_hash == PROTOCOL.protocol_hash
    assert state.protocol.protocol_document_hash == "a" * 64
    assert state.cohort.status is ProspectiveCohortStatus.COLLECTING_TRAINING
    assert state.cohort.training_keys == ((REPOSITORY, 302), (REPOSITORY, 303))
    assert [record.pr_number for record in state.snapshot.records] == [302, 303]
    assert state.snapshot_hash == state.snapshot.snapshot_hash
    assert len(state.state_hash) == 64


def test_state_hash_is_independent_of_input_record_order() -> None:
    first = _record(302, minute=5)
    second = _record(303, minute=10)

    left = advance_prospective_evidence_state_v2(records=(first, second), protocol=PROTOCOL)
    right = advance_prospective_evidence_state_v2(records=(second, first), protocol=PROTOCOL)

    assert left.state_hash == right.state_hash
    assert left.canonical_json() == right.canonical_json()


def test_append_only_advance_chains_state_and_preserves_enrollment() -> None:
    initial = advance_prospective_evidence_state_v2(
        records=(_record(302, minute=5), _record(303, minute=10)),
        protocol=PROTOCOL,
    )
    third = _record(304, minute=20)

    advanced = advance_prospective_evidence_state_v2(
        records=(*initial.snapshot.records, third),
        protocol=PROTOCOL,
        previous_state=initial,
    )

    assert advanced.previous_state_hash == initial.state_hash
    assert advanced.protocol_hash == initial.protocol_hash
    assert advanced.cohort.training_keys == (
        (REPOSITORY, 302),
        (REPOSITORY, 303),
        (REPOSITORY, 304),
    )
    assert advanced.snapshot_hash != initial.snapshot_hash


def test_advance_rejects_a_different_protocol_identity() -> None:
    initial = advance_prospective_evidence_state_v2(
        records=(_record(302, minute=5), _record(303, minute=10)),
        protocol=PROTOCOL,
    )
    other = PROTOCOL.model_copy(update={"protocol_document_hash": "b" * 64})

    with pytest.raises(ValueError, match="protocol identity does not match"):
        advance_prospective_evidence_state_v2(
            records=(*initial.snapshot.records, _record(304, minute=20)),
            protocol=other,
            previous_state=initial,
        )


def test_persisted_state_rejects_cohort_not_matching_protocol() -> None:
    state = advance_prospective_evidence_state_v2(
        records=(_record(302, minute=5), _record(303, minute=10)),
        protocol=PROTOCOL,
    )
    corrupted = state.cohort.model_copy(update={"training_count": 17})

    with pytest.raises(ValueError, match="cohort configuration must match bound protocol"):
        ProspectiveEvidenceStateV2(
            protocol=PROTOCOL,
            snapshot=state.snapshot,
            cohort=corrupted,
        )


def test_previously_collected_evidence_cannot_be_rewritten_silently() -> None:
    first = _record(302, minute=5)
    second = _record(303, minute=10)
    initial = advance_prospective_evidence_state_v2(records=(first, second), protocol=PROTOCOL)
    rewritten_first = first.model_copy(
        update={
            "submitted_reviews": (
                GitHubReviewSourceRecord(
                    review_id=9001,
                    submitted_at=first.opened_at + timedelta(minutes=1),
                    state="commented",
                    actor_key="late-reviewer",
                    source_url="https://example.test/reviews/9001",
                ),
            )
        }
    )

    with pytest.raises(ValueError, match="previously collected evidence cannot be rewritten"):
        advance_prospective_evidence_state_v2(
            records=(rewritten_first, second, _record(304, minute=20)),
            protocol=PROTOCOL,
            previous_state=initial,
        )


def test_previous_source_record_cannot_disappear_from_later_state() -> None:
    first = _record(302, minute=5)
    second = _record(303, minute=10)
    initial = advance_prospective_evidence_state_v2(records=(first, second), protocol=PROTOCOL)

    with pytest.raises(ValueError, match="must retain every previously collected source record"):
        advance_prospective_evidence_state_v2(
            records=(second, _record(304, minute=20)),
            protocol=PROTOCOL,
            previous_state=initial,
        )


def test_registered_study_state_rejects_foreign_repository_records() -> None:
    foreign = GitHubPREvidenceRecordV2(
        repository="other/project",
        pr_number=1,
        opened_at=REGISTERED_AT + timedelta(minutes=1),
        merged_at=REGISTERED_AT + timedelta(minutes=2),
        source_url="https://example.test/other/project/pulls/1",
    )

    with pytest.raises(ValueError, match="registered repository"):
        advance_prospective_evidence_state_v2(
            records=(_record(302, minute=5), foreign),
            protocol=PROTOCOL,
        )


def test_persisted_state_rejects_snapshot_record_omitted_from_all_partitions() -> None:
    state = advance_prospective_evidence_state_v2(
        records=(_record(302, minute=5), _record(303, minute=10)),
        protocol=PROTOCOL,
    )
    corrupted = state.cohort.model_copy(update={"training_keys": ((REPOSITORY, 302),)})

    with pytest.raises(ValueError, match="partition every source snapshot record exactly once"):
        ProspectiveEvidenceStateV2(protocol=PROTOCOL, snapshot=state.snapshot, cohort=corrupted)


def test_persisted_state_rejects_key_present_in_multiple_partitions() -> None:
    state = advance_prospective_evidence_state_v2(
        records=(_record(302, minute=5), _record(303, minute=10)),
        protocol=PROTOCOL,
    )
    duplicated = state.cohort.model_copy(update={"interstitial_keys": ((REPOSITORY, 302),)})

    with pytest.raises(ValueError, match="partition every source snapshot record exactly once"):
        ProspectiveEvidenceStateV2(protocol=PROTOCOL, snapshot=state.snapshot, cohort=duplicated)


def _record(number: int, *, minute: int) -> GitHubPREvidenceRecordV2:
    opened = REGISTERED_AT + timedelta(minutes=minute)
    return GitHubPREvidenceRecordV2(
        repository=REPOSITORY,
        pr_number=number,
        opened_at=opened,
        merged_at=opened + timedelta(minutes=2),
        source_url=f"https://api.github.com/repos/{REPOSITORY}/pulls/{number}",
    )

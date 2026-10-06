from __future__ import annotations

from hashlib import sha256
import json
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

from .prospective_cohort_v2 import ProspectivePRCohortV2, select_prospective_pr_cohort_v2
from .source_evidence_v2 import GitHubPREvidenceRecordV2, GitHubPREvidenceSnapshotV2


PROSPECTIVE_STATE_VERSION = "pr-review-prospective-state/v2"
PROSPECTIVE_PROTOCOL_ID = "pr-review-validation/v2"
Sha256Hex = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]


class ProspectiveEvidenceStateV2(BaseModel):
    """Hash-addressed, append-only evidence/enrollment state for prospective v2.

    The source snapshot contains observable evidence.  The cohort contains the
    persisted enrollment decision.  Chaining each update to the previous state
    makes both source growth and enrollment history auditable without allowing a
    later source refresh to silently rewrite already-used evidence.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    state_version: Literal[PROSPECTIVE_STATE_VERSION] = PROSPECTIVE_STATE_VERSION
    protocol_id: Literal[PROSPECTIVE_PROTOCOL_ID] = PROSPECTIVE_PROTOCOL_ID
    snapshot: GitHubPREvidenceSnapshotV2
    cohort: ProspectivePRCohortV2
    previous_state_hash: Sha256Hex | None = None

    @model_validator(mode="after")
    def validate_binding(self) -> "ProspectiveEvidenceStateV2":
        record_keys = {
            (record.repository, record.pr_number)
            for record in self.snapshot.records
        }
        if any(record.repository != self.cohort.repository for record in self.snapshot.records):
            raise ValueError("all source records must belong to the registered repository")
        if any(
            record.opened_at <= self.cohort.registration_merged_at
            for record in self.snapshot.records
        ):
            raise ValueError("source records must open strictly after registration merge")

        enrolled = set(
            (*self.cohort.training_keys, *self.cohort.interstitial_keys,
             *self.cohort.holdout_keys, *self.cohort.post_holdout_keys)
        )
        if not enrolled.issubset(record_keys):
            raise ValueError("cohort enrollment must be backed by source snapshot records")
        return self

    @property
    def snapshot_hash(self) -> str:
        return self.snapshot.snapshot_hash

    def canonical_payload(self) -> dict[str, object]:
        return {
            "state_version": self.state_version,
            "protocol_id": self.protocol_id,
            "snapshot": self.snapshot.canonical_payload(),
            "cohort": self.cohort.model_dump(mode="json"),
            "previous_state_hash": self.previous_state_hash,
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
    def state_hash(self) -> str:
        return sha256(self.canonical_json().encode("utf-8")).hexdigest()


def advance_prospective_evidence_state_v2(
    *,
    records: tuple[GitHubPREvidenceRecordV2, ...] | list[GitHubPREvidenceRecordV2],
    repository: str,
    registration_merged_at,
    training_count: int,
    holdout_count: int,
    model_frozen_at=None,
    previous_state: ProspectiveEvidenceStateV2 | None = None,
) -> ProspectiveEvidenceStateV2:
    """Create or advance an append-only prospective evidence state.

    Previously collected source records are immutable.  Correcting frozen source
    evidence therefore requires an explicit new protocol/artifact rather than an
    in-place refresh that could change fitted results after they were observed.
    """

    normalized_records = tuple(records)
    if any(record.repository != repository for record in normalized_records):
        raise ValueError("all source records must belong to the registered repository")

    if previous_state is not None:
        _validate_previous_source_evidence(
            previous_state=previous_state,
            records=normalized_records,
            repository=repository,
        )

    snapshot = GitHubPREvidenceSnapshotV2(records=normalized_records)
    cohort = select_prospective_pr_cohort_v2(
        records=snapshot.records,
        repository=repository,
        registration_merged_at=registration_merged_at,
        training_count=training_count,
        holdout_count=holdout_count,
        model_frozen_at=model_frozen_at,
        previous_cohort=(previous_state.cohort if previous_state is not None else None),
    )
    return ProspectiveEvidenceStateV2(
        snapshot=snapshot,
        cohort=cohort,
        previous_state_hash=(previous_state.state_hash if previous_state is not None else None),
    )


def _validate_previous_source_evidence(
    *,
    previous_state: ProspectiveEvidenceStateV2,
    records: tuple[GitHubPREvidenceRecordV2, ...],
    repository: str,
) -> None:
    if previous_state.cohort.repository != repository:
        raise ValueError("previous state repository does not match")

    current = {(record.repository, record.pr_number): record for record in records}
    previous = {
        (record.repository, record.pr_number): record
        for record in previous_state.snapshot.records
    }
    missing = set(previous).difference(current)
    if missing:
        raise ValueError("must retain every previously collected source record")

    for key, old_record in previous.items():
        if old_record.canonical_payload() != current[key].canonical_payload():
            raise ValueError("previously collected evidence cannot be rewritten silently")

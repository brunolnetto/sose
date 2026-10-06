from __future__ import annotations

from hashlib import sha256
import json
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, StringConstraints, model_validator

from .prospective_cohort_v2 import ProspectivePRCohortV2, select_prospective_pr_cohort_v2
from .prospective_protocol_v2 import ProspectiveStudyProtocolV2
from .source_evidence_v2 import GitHubPREvidenceRecordV2, GitHubPREvidenceSnapshotV2


PROSPECTIVE_STATE_VERSION = "pr-review-prospective-state/v2"
Sha256Hex = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]


class ProspectiveEvidenceStateV2(BaseModel):
    """Hash-addressed, append-only evidence/enrollment state for prospective v2.

    The bound protocol identifies both the frozen preregistration document and the
    GitHub merge timestamp that activated enrollment. Source evidence and cohort
    membership can therefore be audited against one immutable study identity.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    state_version: Literal[PROSPECTIVE_STATE_VERSION] = PROSPECTIVE_STATE_VERSION
    protocol: ProspectiveStudyProtocolV2
    snapshot: GitHubPREvidenceSnapshotV2
    cohort: ProspectivePRCohortV2
    previous_state_hash: Sha256Hex | None = None

    @model_validator(mode="after")
    def validate_binding(self) -> "ProspectiveEvidenceStateV2":
        if (
            self.cohort.repository != self.protocol.repository
            or self.cohort.registration_merged_at != self.protocol.registration_merged_at
            or self.cohort.training_count != self.protocol.training_count
            or self.cohort.holdout_count != self.protocol.holdout_count
        ):
            raise ValueError("cohort configuration must match bound protocol")

        record_keys = {
            (record.repository, record.pr_number)
            for record in self.snapshot.records
        }
        if any(record.repository != self.protocol.repository for record in self.snapshot.records):
            raise ValueError("all source records must belong to the registered repository")
        if any(
            record.opened_at <= self.protocol.registration_merged_at
            for record in self.snapshot.records
        ):
            raise ValueError("source records must open strictly after registration merge")

        enrolled = (
            *self.cohort.training_keys,
            *self.cohort.interstitial_keys,
            *self.cohort.holdout_keys,
            *self.cohort.post_holdout_keys,
        )
        enrolled_keys = set(enrolled)
        if len(enrolled) != len(enrolled_keys) or enrolled_keys != record_keys:
            raise ValueError("cohort partitions must partition every source snapshot record exactly once")
        return self

    @property
    def protocol_hash(self) -> str:
        return self.protocol.protocol_hash

    @property
    def snapshot_hash(self) -> str:
        return self.snapshot.snapshot_hash

    def canonical_payload(self) -> dict[str, object]:
        return {
            "state_version": self.state_version,
            "protocol": self.protocol.canonical_payload(),
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
    protocol: ProspectiveStudyProtocolV2,
    model_frozen_at=None,
    previous_state: ProspectiveEvidenceStateV2 | None = None,
) -> ProspectiveEvidenceStateV2:
    """Create or advance evidence under exactly one frozen prospective protocol."""

    normalized_records = tuple(records)
    if any(record.repository != protocol.repository for record in normalized_records):
        raise ValueError("all source records must belong to the registered repository")

    if previous_state is not None:
        if previous_state.protocol_hash != protocol.protocol_hash:
            raise ValueError("protocol identity does not match previous state")
        _validate_previous_source_evidence(
            previous_state=previous_state,
            records=normalized_records,
            repository=protocol.repository,
        )

    snapshot = GitHubPREvidenceSnapshotV2(records=normalized_records)
    cohort = select_prospective_pr_cohort_v2(
        records=snapshot.records,
        repository=protocol.repository,
        registration_merged_at=protocol.registration_merged_at,
        training_count=protocol.training_count,
        holdout_count=protocol.holdout_count,
        model_frozen_at=model_frozen_at,
        previous_cohort=(previous_state.cohort if previous_state is not None else None),
    )
    return ProspectiveEvidenceStateV2(
        protocol=protocol,
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

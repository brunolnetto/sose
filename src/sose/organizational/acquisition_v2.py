from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from hashlib import sha256
import json
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

from .source_builder_v2 import build_github_pr_evidence_v2
from .source_evidence_v2 import GitHubPREvidenceRecordV2, GitHubPREvidenceSnapshotV2


NonBlankString = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
EndpointName = Literal[
    "pull_request",
    "timeline",
    "reviews",
    "workflow_runs",
    "workflow_jobs",
]


class GitHubEndpointCaptureV2(BaseModel):
    """Evidence that one GitHub source endpoint was fetched to exhaustion."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    endpoint: EndpointName
    pages_fetched: int = Field(ge=1)
    record_count: int = Field(ge=0)
    complete: bool
    source_urls: tuple[NonBlankString, ...] = ()
    next_page_url: NonBlankString | None = None

    @model_validator(mode="after")
    def validate_pagination(self) -> "GitHubEndpointCaptureV2":
        if not self.source_urls:
            raise ValueError("source_urls must identify fetched pages")
        canonical_urls = tuple(sorted(set(self.source_urls)))
        if len(canonical_urls) != self.pages_fetched:
            raise ValueError("pages_fetched must match distinct source_urls")
        if self.complete and self.next_page_url is not None:
            raise ValueError("complete acquisition cannot retain next_page_url")
        if not self.complete and self.next_page_url is None:
            raise ValueError("incomplete acquisition requires next_page_url")
        object.__setattr__(self, "source_urls", canonical_urls)
        return self

    def canonical_payload(self) -> dict[str, object]:
        return {
            "endpoint": self.endpoint,
            "pages_fetched": self.pages_fetched,
            "record_count": self.record_count,
            "complete": self.complete,
            "source_urls": list(self.source_urls),
            "next_page_url": self.next_page_url,
        }


class GitHubPRAcquisitionManifestV2(BaseModel):
    """Completeness proof for the raw GitHub payloads used for one PR record."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    repository: NonBlankString
    pr_number: int = Field(ge=1)
    captured_at: datetime
    pull_request: GitHubEndpointCaptureV2
    timeline: GitHubEndpointCaptureV2
    reviews: GitHubEndpointCaptureV2
    workflow_runs: GitHubEndpointCaptureV2
    workflow_jobs: GitHubEndpointCaptureV2

    @model_validator(mode="after")
    def validate_manifest(self) -> "GitHubPRAcquisitionManifestV2":
        if self.captured_at.tzinfo is None or self.captured_at.utcoffset() is None:
            raise ValueError("captured_at must be timezone-aware")
        expected = {
            "pull_request": self.pull_request,
            "timeline": self.timeline,
            "reviews": self.reviews,
            "workflow_runs": self.workflow_runs,
            "workflow_jobs": self.workflow_jobs,
        }
        for name, capture in expected.items():
            if capture.endpoint != name:
                raise ValueError(f"{name} capture must declare endpoint={name}")
        return self

    @property
    def is_complete(self) -> bool:
        return all(
            capture.complete
            for capture in (
                self.pull_request,
                self.timeline,
                self.reviews,
                self.workflow_runs,
                self.workflow_jobs,
            )
        )

    def require_complete(self) -> None:
        for name, capture in (
            ("pull_request", self.pull_request),
            ("timeline", self.timeline),
            ("reviews", self.reviews),
            ("workflow_runs", self.workflow_runs),
            ("workflow_jobs", self.workflow_jobs),
        ):
            if not capture.complete:
                raise ValueError(f"{name} acquisition is incomplete")

    def canonical_payload(self) -> dict[str, object]:
        return {
            "repository": self.repository,
            "pr_number": self.pr_number,
            "captured_at": self.captured_at.astimezone(UTC).isoformat(),
            "pull_request": self.pull_request.canonical_payload(),
            "timeline": self.timeline.canonical_payload(),
            "reviews": self.reviews.canonical_payload(),
            "workflow_runs": self.workflow_runs.canonical_payload(),
            "workflow_jobs": self.workflow_jobs.canonical_payload(),
        }


class GitHubPRAcquisitionArtifactV2(BaseModel):
    """Hash-addressed pairing of acquisition completeness and normalized evidence."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    manifest: GitHubPRAcquisitionManifestV2
    evidence: GitHubPREvidenceRecordV2

    @model_validator(mode="after")
    def validate_identity(self) -> "GitHubPRAcquisitionArtifactV2":
        if (
            self.manifest.repository != self.evidence.repository
            or self.manifest.pr_number != self.evidence.pr_number
        ):
            raise ValueError("manifest pull request identity does not match evidence")
        if not self.manifest.is_complete:
            raise ValueError("acquisition artifact requires complete source acquisition")
        return self

    def canonical_payload(self) -> dict[str, object]:
        return {
            "manifest": self.manifest.canonical_payload(),
            "evidence": self.evidence.canonical_payload(),
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
    def artifact_hash(self) -> str:
        return sha256(self.canonical_json().encode("utf-8")).hexdigest()


class GitHubPRAcquisitionSnapshotV2(BaseModel):
    """Canonical acquisition snapshot that preserves completeness provenance."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    artifacts: tuple[GitHubPRAcquisitionArtifactV2, ...] = Field(min_length=2)

    @model_validator(mode="after")
    def canonicalize_and_validate(self) -> "GitHubPRAcquisitionSnapshotV2":
        keys = tuple(
            (artifact.evidence.repository, artifact.evidence.pr_number)
            for artifact in self.artifacts
        )
        if len(keys) != len(set(keys)):
            raise ValueError("duplicate GitHub pull request acquisition identity")
        if any(not artifact.manifest.is_complete for artifact in self.artifacts):
            raise ValueError("all acquisition artifacts must be complete")
        object.__setattr__(
            self,
            "artifacts",
            tuple(
                sorted(
                    self.artifacts,
                    key=lambda artifact: (
                        artifact.evidence.opened_at,
                        artifact.evidence.repository,
                        artifact.evidence.pr_number,
                    ),
                )
            ),
        )
        return self

    def canonical_payload(self) -> dict[str, object]:
        return {"artifacts": [artifact.canonical_payload() for artifact in self.artifacts]}

    def canonical_json(self) -> str:
        return json.dumps(
            self.canonical_payload(),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        )

    @property
    def acquisition_hash(self) -> str:
        return sha256(self.canonical_json().encode("utf-8")).hexdigest()

    def to_evidence_snapshot(self) -> GitHubPREvidenceSnapshotV2:
        return GitHubPREvidenceSnapshotV2(
            records=tuple(artifact.evidence for artifact in self.artifacts)
        )


def build_complete_github_pr_evidence_v2(
    *,
    manifest: GitHubPRAcquisitionManifestV2,
    pull_request: Mapping[str, Any],
    timeline_events: Sequence[Mapping[str, Any]] = (),
    reviews: Sequence[Mapping[str, Any]] = (),
    workflow_jobs: Sequence[Mapping[str, Any]] = (),
    workflow_runs: Sequence[Mapping[str, Any]] = (),
) -> GitHubPRAcquisitionArtifactV2:
    """Normalize one PR only after proving all required source collections complete."""

    manifest.require_complete()
    _require_count("pull_request", manifest.pull_request.record_count, 1)
    _require_count("timeline", manifest.timeline.record_count, len(timeline_events))
    _require_count("reviews", manifest.reviews.record_count, len(reviews))
    _require_count("workflow_runs", manifest.workflow_runs.record_count, len(workflow_runs))
    _require_count("workflow_jobs", manifest.workflow_jobs.record_count, len(workflow_jobs))

    raw_number = pull_request.get("number")
    if raw_number != manifest.pr_number:
        raise ValueError("manifest pull request identity does not match source payload")
    source_repository = _pull_request_repository(pull_request)
    if source_repository is not None and source_repository != manifest.repository:
        raise ValueError("manifest repository does not match pull request provenance")
    _require_workflow_job_membership(workflow_runs=workflow_runs, workflow_jobs=workflow_jobs)

    evidence = build_github_pr_evidence_v2(
        repository=manifest.repository,
        pull_request=pull_request,
        timeline_events=timeline_events,
        reviews=reviews,
        workflow_jobs=workflow_jobs,
        workflow_runs=workflow_runs,
    )
    return GitHubPRAcquisitionArtifactV2(manifest=manifest, evidence=evidence)


def build_evidence_snapshot_from_acquisitions_v2(
    artifacts: Sequence[GitHubPRAcquisitionArtifactV2],
) -> GitHubPREvidenceSnapshotV2:
    """Project complete acquisition artifacts into the frozen v2 evidence schema."""

    return GitHubPRAcquisitionSnapshotV2(artifacts=tuple(artifacts)).to_evidence_snapshot()


def _pull_request_repository(pull_request: Mapping[str, Any]) -> str | None:
    base = pull_request.get("base")
    if not isinstance(base, Mapping):
        return None
    repository = base.get("repo")
    if not isinstance(repository, Mapping):
        return None
    full_name = repository.get("full_name")
    if not isinstance(full_name, str) or not full_name.strip():
        return None
    return full_name.strip()


def _require_workflow_job_membership(
    *,
    workflow_runs: Sequence[Mapping[str, Any]],
    workflow_jobs: Sequence[Mapping[str, Any]],
) -> None:
    run_ids = {
        run_id
        for run in workflow_runs
        if isinstance((run_id := run.get("id")), int) and not isinstance(run_id, bool)
    }
    for job in workflow_jobs:
        run_id = job.get("run_id")
        if run_id not in run_ids:
            raise ValueError(f"workflow job run_id={run_id} was not acquired")


def _require_count(endpoint: str, expected: int, actual: int) -> None:
    if expected != actual:
        raise ValueError(f"{endpoint} record_count={expected} but received {actual} payloads")

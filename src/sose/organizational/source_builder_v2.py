from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Any

from .source_evidence_v2 import (
    GitHubPREvidenceRecordV2,
    GitHubReviewSourceRecord,
    GitHubReviewTimelineSourceRecord,
    GitHubWorkflowJobSourceRecordV2,
)


JsonMapping = Mapping[str, Any]


def build_github_pr_evidence_v2(
    *,
    repository: str,
    pull_request: JsonMapping,
    timeline_events: Sequence[JsonMapping] = (),
    reviews: Sequence[JsonMapping] = (),
    workflow_jobs: Sequence[JsonMapping] = (),
) -> GitHubPREvidenceRecordV2:
    """Build one prospective v2 source record from already-fetched GitHub payloads.

    This function performs no network access and no inference of actor effort.  It
    only preserves source identities required by the prospective protocol and
    rejects incomplete evidence before it can enter a hash-addressed snapshot.
    """

    if not isinstance(repository, str) or not repository.strip():
        raise ValueError("repository must be a non-empty string")

    pr_number = _required_int(pull_request, "number")
    opened_at = _required_datetime(pull_request, "created_at")
    merged_at = _required_datetime(pull_request, "merged_at")
    source_url = _source_url(pull_request)
    author_actor_key = _login(pull_request.get("user"))
    author_is_bot = _is_bot(pull_request.get("user"), login=author_actor_key)

    supported_timeline = tuple(
        _timeline_record(event)
        for event in timeline_events
        if event.get("event") in {"review_requested", "review_request_removed"}
    )
    submitted_reviews = tuple(_review_record(review) for review in reviews)
    jobs = tuple(_workflow_job_record(job) for job in workflow_jobs)

    return GitHubPREvidenceRecordV2(
        repository=repository.strip(),
        pr_number=pr_number,
        opened_at=opened_at,
        merged_at=merged_at,
        source_url=source_url,
        author_actor_key=author_actor_key,
        author_is_bot=author_is_bot,
        workflow_jobs=jobs,
        review_timeline=supported_timeline,
        submitted_reviews=submitted_reviews,
    )


def _timeline_record(payload: JsonMapping) -> GitHubReviewTimelineSourceRecord:
    event = payload.get("event")
    if event not in {"review_requested", "review_request_removed"}:
        raise ValueError("review timeline event must be review_requested or review_request_removed")
    return GitHubReviewTimelineSourceRecord(
        event_id=_required_int(payload, "id"),
        event=event,
        occurred_at=_required_datetime(payload, "created_at"),
        source_url=_source_url(payload),
        requested_actor_key=_requested_actor(payload),
    )


def _review_record(payload: JsonMapping) -> GitHubReviewSourceRecord:
    state = _required_string(payload, "state")
    return GitHubReviewSourceRecord(
        review_id=_required_int(payload, "id"),
        submitted_at=_required_datetime(payload, "submitted_at"),
        state=state,
        actor_key=_login(payload.get("user")),
        source_url=_source_url(payload),
    )


def _workflow_job_record(payload: JsonMapping) -> GitHubWorkflowJobSourceRecordV2:
    gate_evidence = payload.get("gate_evidence_url")
    return GitHubWorkflowJobSourceRecordV2(
        job_id=_required_int(payload, "id"),
        workflow_id=_required_int(payload, "workflow_id"),
        run_id=_required_int(payload, "run_id"),
        run_attempt=_required_int(payload, "run_attempt"),
        name=_required_string(payload, "name"),
        started_at=_required_datetime(payload, "started_at"),
        completed_at=_required_datetime(payload, "completed_at"),
        conclusion=_required_string(payload, "conclusion"),
        source_url=_source_url(payload),
        is_gate=payload.get("is_gate") is True,
        gate_evidence_url=(
            gate_evidence.strip()
            if isinstance(gate_evidence, str) and gate_evidence.strip()
            else None
        ),
    )


def _requested_actor(payload: JsonMapping) -> str | None:
    reviewer = _login(payload.get("requested_reviewer"))
    if reviewer is not None:
        return reviewer
    team = payload.get("requested_team")
    if not isinstance(team, Mapping):
        return None
    slug = team.get("slug") or team.get("name")
    if not isinstance(slug, str) or not slug.strip():
        return None
    return f"team:{slug.strip()}"


def _is_bot(value: object, *, login: str | None) -> bool:
    if isinstance(value, Mapping):
        actor_type = value.get("type")
        if isinstance(actor_type, str) and actor_type.casefold() == "bot":
            return True
    return login is not None and login.casefold().endswith("[bot]")


def _login(value: object) -> str | None:
    if not isinstance(value, Mapping):
        return None
    login = value.get("login")
    if not isinstance(login, str) or not login.strip():
        return None
    return login.strip()


def _source_url(payload: JsonMapping) -> str:
    for field in ("url", "html_url", "source_url"):
        value = payload.get(field)
        if isinstance(value, str) and value.strip():
            return value.strip()
    raise ValueError("GitHub source payload requires url or html_url")


def _required_string(payload: JsonMapping, field: str) -> str:
    value = payload.get(field)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"GitHub source payload requires non-empty {field}")
    return value.strip()


def _required_int(payload: JsonMapping, field: str) -> int:
    value = payload.get(field)
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(f"GitHub source payload requires positive integer {field}")
    return value


def _required_datetime(payload: JsonMapping, field: str) -> datetime:
    value = payload.get(field)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"GitHub source payload requires {field}")
    normalized = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise ValueError(f"GitHub source payload requires ISO-8601 {field}") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"GitHub source payload requires timezone-aware {field}")
    return parsed

from __future__ import annotations

from collections.abc import Iterator, Mapping, Sequence
from datetime import datetime
from typing import Any

from .observations import ObservedEventKind, ObservedPREvent, ObservedPRTrace


JsonMapping = Mapping[str, Any]


def normalize_github_pr_trace(
    *,
    repository: str,
    pull_request: JsonMapping,
    timeline_events: Sequence[JsonMapping] = (),
    reviews: Sequence[JsonMapping] = (),
    workflow_jobs: Sequence[JsonMapping] = (),
) -> ObservedPRTrace:
    """Normalize already-fetched GitHub payloads without inferring actor effort.

    The adapter is deliberately pure/offline: fetching, pagination and credentials
    remain outside the organizational model. Endpoint payloads are reduced to
    source-preserving events that can be serialized and replayed deterministically.
    """

    pr_number = _required_int(pull_request, "number")
    opened_at = _required_datetime(pull_request, "created_at")
    merged_at = _optional_datetime(pull_request.get("merged_at"))
    closed_at = _optional_datetime(pull_request.get("closed_at"))
    terminal_at = merged_at if merged_at is not None else closed_at

    staged: list[tuple[datetime, int, int, ObservedPREvent]] = []
    sources = (
        _opened_events(pull_request, pr_number, opened_at),
        _timeline_events(timeline_events),
        _review_events(reviews),
        _workflow_job_events(workflow_jobs),
        _terminal_events(pull_request, pr_number, merged_at, terminal_at),
    )
    for source in sources:
        for event, priority in source:
            if terminal_at is not None and event.occurred_at > terminal_at:
                continue
            staged.append((event.occurred_at, priority, len(staged), event))

    ordered = tuple(
        event
        for _, _, _, event in sorted(
            staged,
            key=lambda item: (item[0], item[1], item[2]),
        )
    )
    return ObservedPRTrace(repository=repository, pr_number=pr_number, events=ordered)


_Staged = Iterator[tuple[ObservedPREvent, int]]


def _opened_events(pull_request: JsonMapping, pr_number: int, opened_at: datetime) -> _Staged:
    opened_metadata: dict[str, object] = {}
    if isinstance(pull_request.get("author_is_bot"), bool):
        opened_metadata["author_is_bot"] = pull_request["author_is_bot"]
    yield (
        ObservedPREvent(
            source_event_id=f"github:pr:{pr_number}:opened",
            kind=ObservedEventKind.OPENED,
            occurred_at=opened_at,
            actor_key=_login(pull_request.get("user")),
            metadata=opened_metadata,
        ),
        0,
    )


def _timeline_events(timeline_events: Sequence[JsonMapping]) -> _Staged:
    for timeline in timeline_events:
        timeline_kind = timeline.get("event")
        if timeline_kind not in {"review_requested", "review_request_removed"}:
            continue
        occurred_at = _optional_datetime(timeline.get("created_at"))
        event_id = timeline.get("id")
        if occurred_at is None or event_id is None:
            continue
        reviewer = _requested_actor(timeline)
        kind = (
            ObservedEventKind.REVIEW_REQUESTED
            if timeline_kind == "review_requested"
            else ObservedEventKind.REVIEW_REQUEST_REMOVED
        )
        yield (
            ObservedPREvent(
                source_event_id=f"github:timeline:{event_id}:{timeline_kind}",
                kind=kind,
                occurred_at=occurred_at,
                actor_key=reviewer,
            ),
            20,
        )


def _review_events(reviews: Sequence[JsonMapping]) -> _Staged:
    for review in reviews:
        submitted_at = _optional_datetime(review.get("submitted_at"))
        review_id = review.get("id")
        if submitted_at is None or review_id is None:
            continue
        yield (
            ObservedPREvent(
                source_event_id=f"github:review:{review_id}",
                kind=ObservedEventKind.REVIEW_SUBMITTED,
                occurred_at=submitted_at,
                actor_key=_login(review.get("user")),
                state=_lowered(review.get("state")),
            ),
            30,
        )


def _workflow_job_events(workflow_jobs: Sequence[JsonMapping]) -> _Staged:
    for job in workflow_jobs:
        job_id = job.get("id")
        if job_id is None:
            continue
        started_at = _optional_datetime(job.get("started_at"))
        completed_at = _optional_datetime(job.get("completed_at"))
        common_metadata = _job_metadata(job, job_id)
        if started_at is not None:
            yield (
                ObservedPREvent(
                    source_event_id=f"github:job:{job_id}:started",
                    kind=ObservedEventKind.CI_STARTED,
                    occurred_at=started_at,
                    metadata=common_metadata,
                ),
                10,
            )
        if completed_at is not None:
            conclusion = job.get("conclusion")
            yield (
                ObservedPREvent(
                    source_event_id=f"github:job:{job_id}:completed",
                    kind=ObservedEventKind.CI_COMPLETED,
                    occurred_at=completed_at,
                    state=_lowered(conclusion),
                    metadata={**common_metadata, "conclusion": conclusion},
                ),
                40,
            )


def _job_metadata(job: JsonMapping, job_id: object) -> dict[str, object]:
    name = job.get("name")
    job_name = name if isinstance(name, str) and name.strip() else None
    gate_evidence = job.get("gate_evidence_url")
    is_gate = job.get("is_gate") is True
    common_metadata: dict[str, object] = {"name": job_name, "ci_job_id": job_id}
    for key in ("workflow_id", "run_id", "run_attempt"):
        value = job.get(key)
        if isinstance(value, int) and not isinstance(value, bool):
            common_metadata[key] = value
    if is_gate and isinstance(gate_evidence, str) and gate_evidence.strip():
        common_metadata.update(
            {
                "ci_gate": True,
                "ci_gate_evidence": gate_evidence.strip(),
            }
        )
    return common_metadata


def _terminal_events(
    pull_request: JsonMapping,
    pr_number: int,
    merged_at: datetime | None,
    terminal_at: datetime | None,
) -> _Staged:
    if terminal_at is None:
        return
    if merged_at is not None:
        kind = ObservedEventKind.MERGED
        actor = _login(pull_request.get("merged_by"))
        suffix = "merged"
    else:
        kind = ObservedEventKind.CLOSED
        actor = None
        suffix = "closed"
    yield (
        ObservedPREvent(
            source_event_id=f"github:pr:{pr_number}:{suffix}",
            kind=kind,
            occurred_at=terminal_at,
            actor_key=actor,
        ),
        100,
    )


def _lowered(value: object) -> str | None:
    return value.lower() if isinstance(value, str) and value.strip() else None


def _requested_actor(timeline: JsonMapping) -> str | None:
    reviewer = _login(timeline.get("requested_reviewer"))
    if reviewer is not None:
        return reviewer
    team = timeline.get("requested_team")
    if not isinstance(team, Mapping):
        return None
    slug = team.get("slug") or team.get("name")
    return f"team:{slug}" if isinstance(slug, str) and slug.strip() else None


def _required_int(payload: JsonMapping, field: str) -> int:
    value = payload.get(field)
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"GitHub pull request requires integer {field}")
    return value


def _required_datetime(payload: JsonMapping, field: str) -> datetime:
    value = _optional_datetime(payload.get(field))
    if value is None:
        raise ValueError(f"GitHub pull request requires {field}")
    return value


def _optional_datetime(value: object) -> datetime | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise ValueError("GitHub timestamp must be a non-empty ISO-8601 string")
    normalized = value[:-1] + "+00:00" if value.endswith("Z") else value
    parsed = datetime.fromisoformat(normalized)
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("GitHub timestamp must include a timezone")
    return parsed


def _login(value: object) -> str | None:
    if not isinstance(value, Mapping):
        return None
    login = value.get("login")
    if not isinstance(login, str) or not login.strip():
        return None
    return login.strip()

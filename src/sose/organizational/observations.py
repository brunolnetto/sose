from __future__ import annotations

from collections import defaultdict, deque
from collections.abc import Mapping
from datetime import datetime
from enum import StrEnum
import json
from types import MappingProxyType
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, field_validator, model_validator


NonBlankString = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]


class ObservedEventKind(StrEnum):
    OPENED = "opened"
    CLOSED = "closed"
    MERGED = "merged"
    REVIEW_REQUESTED = "review_requested"
    REVIEW_SUBMITTED = "review_submitted"
    ASSIGNED = "assigned"
    UNASSIGNED = "unassigned"
    LABELED = "labeled"
    UNLABELED = "unlabeled"
    COMMIT_PUSHED = "commit_pushed"
    CI_STARTED = "ci_started"
    CI_COMPLETED = "ci_completed"


class ObservedPREvent(BaseModel):
    """Source-preserving normalized observation; contains no inferred actor-time."""

    model_config = ConfigDict(extra="forbid", frozen=True, arbitrary_types_allowed=True)

    source_event_id: NonBlankString
    kind: ObservedEventKind
    occurred_at: datetime
    actor_key: NonBlankString | None = None
    state: NonBlankString | None = None
    metadata: dict[str, object] = Field(default_factory=dict)

    @field_validator("occurred_at")
    @classmethod
    def require_timezone(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("occurred_at must be timezone-aware")
        return value

    @model_validator(mode="after")
    def freeze_metadata(self) -> "ObservedPREvent":
        try:
            encoded = json.dumps(
                _thaw_json(self.metadata),
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            )
        except (TypeError, ValueError) as exc:
            raise ValueError("metadata must contain finite JSON-compatible data") from exc
        object.__setattr__(self, "metadata", _freeze_json(json.loads(encoded)))
        return self

    def canonical_payload(self) -> dict[str, object]:
        return {
            "source_event_id": self.source_event_id,
            "kind": self.kind.value,
            "occurred_at": self.occurred_at.isoformat(),
            "actor_key": self.actor_key,
            "state": self.state,
            "metadata": _thaw_json(self.metadata),
        }


class ObservedPRTrace(BaseModel):
    """Normalized observable event history for one pull request."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    repository: NonBlankString
    pr_number: int = Field(ge=1)
    events: tuple[ObservedPREvent, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_trace(self) -> "ObservedPRTrace":
        ids = [event.source_event_id for event in self.events]
        if len(ids) != len(set(ids)):
            raise ValueError("duplicate source_event_id in observed trace")

        # Python's stable sort preserves source sequence among equal timestamps.
        # That order is semantically relevant when source timestamps are coarse.
        ordered = tuple(sorted(self.events, key=lambda event: event.occurred_at))
        opened = [event for event in ordered if event.kind is ObservedEventKind.OPENED]
        if len(opened) != 1:
            raise ValueError("observed trace requires exactly one opened event")
        open_time = opened[0].occurred_at
        if any(event.occurred_at < open_time for event in ordered):
            raise ValueError("observed trace contains event before opened event")

        terminal = [
            event
            for event in ordered
            if event.kind in {ObservedEventKind.CLOSED, ObservedEventKind.MERGED}
        ]
        if len(terminal) > 1:
            raise ValueError("observed trace allows at most one terminal event")
        if terminal:
            terminal_time = terminal[0].occurred_at
            if terminal_time < open_time:
                raise ValueError("terminal event cannot precede opened event")
            if any(event.occurred_at > terminal_time for event in ordered):
                raise ValueError("observed trace contains event after terminal event")

        object.__setattr__(self, "events", ordered)
        return self

    @property
    def opened_at(self) -> datetime:
        return next(event.occurred_at for event in self.events if event.kind is ObservedEventKind.OPENED)

    @property
    def terminal_at(self) -> datetime | None:
        return next(
            (
                event.occurred_at
                for event in self.events
                if event.kind in {ObservedEventKind.CLOSED, ObservedEventKind.MERGED}
            ),
            None,
        )

    @property
    def lead_time_seconds(self) -> float | None:
        terminal = self.terminal_at
        if terminal is None:
            return None
        return (terminal - self.opened_at).total_seconds()

    def review_response_latencies_seconds(self) -> tuple[float, ...]:
        pending: dict[str, deque[datetime]] = defaultdict(deque)
        latencies: list[float] = []
        for event in self.events:
            actor = event.actor_key
            if actor is None:
                continue
            if event.kind is ObservedEventKind.REVIEW_REQUESTED:
                pending[actor].append(event.occurred_at)
                continue
            if event.kind is not ObservedEventKind.REVIEW_SUBMITTED or not pending[actor]:
                continue
            requested_at = pending[actor].popleft()
            latencies.append((event.occurred_at - requested_at).total_seconds())
        return tuple(latencies)

    def canonical_payload(self) -> dict[str, object]:
        return {
            "repository": self.repository,
            "pr_number": self.pr_number,
            "events": [event.canonical_payload() for event in self.events],
        }

    def canonical_json(self) -> str:
        return json.dumps(
            self.canonical_payload(),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        )


def _freeze_json(value: object) -> object:
    if isinstance(value, dict):
        return MappingProxyType({key: _freeze_json(item) for key, item in value.items()})
    if isinstance(value, list):
        return tuple(_freeze_json(item) for item in value)
    return value


def _thaw_json(value: object) -> object:
    if isinstance(value, Mapping):
        return {str(key): _thaw_json(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_thaw_json(item) for item in value]
    return value

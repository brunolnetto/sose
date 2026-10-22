"""Offline, non-authorizing classification of legacy OD-2 actor admissions.

An unsigned event or mutable projection is evidence for migration review,
NOT a capability to grant work, replacement or paid cancellation.
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from hashlib import sha256
from json import dumps

from sose.core.identity import deterministic_id
from sose.organizational.od2_actor import (
    ACTOR_KIND, ActorAvailabilityCalendar, ActorAvailabilityInterval,
)


@dataclass(frozen=True, slots=True)
class LegacyActorOriginAudit:
    actor_id: str
    classification: str
    missing_work_id_assignments: int
    reason: str

    @property
    def admits_transition(self) -> bool:
        """Never convert a legacy observation into operational authority."""
        return False


def audit_legacy_actor_origin(actor, events) -> LegacyActorOriginAudit:
    """Classify OLD records without changing them or inferring an owner.

    This is an offline preflight. Do not mix snapshots read across concurrent
    writers or use results as an authorization source. No write is performed.
    """
    if actor.entity_type != ACTOR_KIND:
        raise ValueError("expected organizational actor")
    owned_events = tuple(event for event in events if (
        event.entity_id == actor.id and event.entity_type == ACTOR_KIND
    ))
    if any(not isinstance(event.payload, Mapping) for event in owned_events):
        return LegacyActorOriginAudit(
            actor.id, "conflicting_legacy_marker", 0,
            "non-mapping durable actor event payload cannot be migration evidence",
        )
    missing_work = sum(
        event.name == "entity.state_transition"
        and event.payload.get("trigger") == "assign"
        and "assigned_work_id" not in event.payload
        for event in owned_events
    )
    attrs = actor.attributes
    if not isinstance(attrs, Mapping):
        return LegacyActorOriginAudit(
            actor.id, "conflicting_legacy_marker", missing_work,
            "actor attributes are not a mapping and cannot establish provenance",
        )
    # Two mutually missing fields do not establish historical provenance.
    if any(
        not isinstance(value, str) or not value.strip()
        for value in (
            attrs.get("owner_id"), attrs.get("model_spec_hash"),
        )
    ):
        return LegacyActorOriginAudit(
            actor.id, "conflicting_legacy_marker", missing_work,
            "missing or malformed owner/model provenance identity",
        )
    origin_id = deterministic_id("od2-actor-calendar-admitted", actor.id)
    markers = tuple(x for x in owned_events if (
        x.event_id == origin_id or x.name == "od2.actor_calendar_admitted"
    ))
    if not markers:
        return LegacyActorOriginAudit(
            actor.id, "quarantined_unattested", missing_work,
            "no independently recorded admission; projection cannot backfill owner",
        )
    if len(markers) != 1:
        return LegacyActorOriginAudit(
            actor.id, "conflicting_legacy_marker", missing_work,
            "ambiguous original admission events",
        )
    marker = markers[0]
    saved = attrs.get("availability_calendar")
    source = marker.payload.get("calendar_source")
    if not isinstance(saved, dict) or not isinstance(source, dict):
        return LegacyActorOriginAudit(
            actor.id, "conflicting_legacy_marker", missing_work,
            "calendar source or persisted actor projection unavailable",
        )
    try:
        expected_digest = sha256(dumps(
            source, sort_keys=True, separators=(",", ":"), allow_nan=False,
        ).encode("utf-8")).hexdigest()
        # Structural/semantic validation is independent of the digest. A
        # self-consistent but invalid or future-schema calendar is NOT origin
        # evidence and must never upgrade a legacy actor.
        if set(source) != {"version", "config_epoch", "intervals"}:
            raise ValueError("legacy calendar shape mismatch")
        raw_intervals = source["intervals"]
        if not isinstance(raw_intervals, list):
            raise ValueError("legacy calendar intervals must be a list")
        intervals = []
        for raw in raw_intervals:
            if not isinstance(raw, dict) or set(raw) != {"start", "end"}:
                raise ValueError("legacy calendar interval shape mismatch")
            intervals.append(ActorAvailabilityInterval(
                datetime.fromisoformat(raw["start"]),
                datetime.fromisoformat(raw["end"]),
            ))
        calendar = ActorAvailabilityCalendar(
            version=source["version"], config_epoch=source["config_epoch"],
            intervals=tuple(intervals),
        )
        if (
            calendar.snapshot() != source
            or type(attrs.get("config_epoch")) is not int
            or calendar.config_epoch != attrs["config_epoch"]
            or calendar.digest() != expected_digest
        ):
            raise ValueError("legacy calendar schema or epoch mismatch")
    except (TypeError, ValueError, OverflowError, KeyError, AttributeError):
        return LegacyActorOriginAudit(
            actor.id, "conflicting_legacy_marker", missing_work,
            "noncanonical historical calendar source cannot be hashed",
        )
    matched = (
        marker.event_id == origin_id
        and marker.name == "od2.actor_calendar_admitted"
        and marker.entity_type == ACTOR_KIND
        and marker.entity_id == actor.id
        and marker.owner_id == attrs.get("owner_id")
        and type(marker.payload.get("config_epoch")) is int
        and marker.payload.get("config_epoch") == attrs.get("config_epoch")
        and marker.payload.get("model_spec_hash") == attrs.get("model_spec_hash")
        and marker.payload.get("calendar_digest") == expected_digest
        and saved.get("source_digest") == expected_digest
        and set(saved) == {"version", "config_epoch", "intervals", "source_digest"}
        and type(saved.get("version")) is int
        and type(saved.get("config_epoch")) is int
        and {k: saved[k] for k in ("version", "config_epoch", "intervals")} == source
    )
    return LegacyActorOriginAudit(
        actor.id,
        "legacy_calendar_evidence_only" if matched else "conflicting_legacy_marker",
        missing_work,
        (
            "old calendar marker matches; no admission timestamp, replacement or work-ID cutover authority"
            if matched else
            "old calendar marker contradicts mutable projection"
        ),
    )


def audit_legacy_actor_origins(actors, events) -> tuple[LegacyActorOriginAudit, ...]:
    """Caller supplies a coherent snapshot; this probe never writes records."""
    events = tuple(events)
    return tuple(
        audit_legacy_actor_origin(actor, events)
        for actor in sorted(actors, key=lambda x: x.id)
        if actor.entity_type == ACTOR_KIND
    )

"""Bounded #495 static calendar schema + repo-pinned source provenance.

This certifies only the bytes of one reviewed, shipped UTC calendar and the
shape of its weekly windows. It is *not* a runtime availability, admission,
cost, physical-grant or time-zone conversion policy. Those remain #496/#477.
Caller-provided 64-character hashes alone are never treated as attestation.
"""
from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
import json
import re

from .organization import OrganizationDefinition, OperationalConfigReference


@dataclass(frozen=True, slots=True)
class CalendarWindow:
    """weekday follows Python datetime.weekday(): MONDAY=0 ... SUNDAY=6."""

    weekday: int
    start: str
    end: str


@dataclass(frozen=True, slots=True)
class StaticCalendarSource:
    reference_id: str
    reference_version: int
    config_epoch: int
    timezone: str
    windows: tuple[CalendarWindow, ...]
    source_sha256: str


def _unique_pairs(pairs: list[tuple[str, object]]) -> dict:
    result = {}
    for k, v in pairs:
        if k in result:
            raise ValueError("duplicate calendar JSON key")
        result[k] = v
    return result


_CLOCK = re.compile(r"(?:[01][0-9]|2[0-3]):[0-5][0-9]\Z")
# Normative weekday convention: Monday=0, Tuesday=1, ... Sunday=6.
# This is the Python datetime.weekday() convention, NOT JS getDay().
WEEKDAYS_MONDAY_ZERO = (
    "monday", "tuesday", "wednesday", "thursday",
    "friday", "saturday", "sunday",
)
_EXPECTED_KEYS = {
    "schema_version", "reference_id", "reference_version",
    "config_epoch", "timezone", "windows",
}
_WINDOW_KEYS = {"weekday", "start", "end"}


def parse_static_calendar_source(source_bytes: bytes) -> StaticCalendarSource:
    """Falsifiable closed schema, no arbitrary code or config evaluation."""
    if not isinstance(source_bytes, bytes):
        raise TypeError("calendar source must be exact immutable UTF-8 bytes")
    try:
        data = json.loads(source_bytes.decode("utf-8"), object_pairs_hook=_unique_pairs)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("invalid calendar source bytes") from exc
    if not isinstance(data, dict) or set(data) != _EXPECTED_KEYS:
        raise ValueError("calendar fields must exactly match schema v1")
    if type(data["schema_version"]) is not int or data["schema_version"] != 1:
        raise ValueError("unsupported calendar schema_version")
    if not isinstance(data["reference_id"], str) or not data["reference_id"].strip():
        raise ValueError("invalid calendar reference_id")
    if type(data["reference_version"]) is not int or data["reference_version"] != 1:
        raise ValueError("unsupported calendar reference_version")
    if type(data["config_epoch"]) is not int or data["config_epoch"] < 0:
        raise ValueError("invalid calendar config_epoch")
    if data["timezone"] != "UTC":
        raise ValueError("static v1 calendar requires explicit UTC; no implicit time-zone policy")
    raw_windows = data["windows"]
    if not isinstance(raw_windows, list) or not raw_windows:
        raise ValueError("calendar windows must be a nonempty array")
    windows = []
    for row in raw_windows:
        if not isinstance(row, dict) or set(row) != _WINDOW_KEYS:
            raise ValueError("invalid calendar window fields")
        day, start, end = row["weekday"], row["start"], row["end"]
        if type(day) is not int or not 0 <= day <= 6:
            raise ValueError("calendar weekday must be Monday=0 through Sunday=6")
        if (
            not isinstance(start, str) or not isinstance(end, str)
            or _CLOCK.fullmatch(start) is None or _CLOCK.fullmatch(end) is None
        ):
            raise ValueError("calendar time requires HH:MM (UTC) with exact minutes")
        if start >= end:
            raise ValueError("calendar window must have positive duration within a day")
        windows.append(CalendarWindow(day, start, end))
    if windows != sorted(windows, key=lambda w: (w.weekday, w.start, w.end)):
        raise ValueError("calendar windows must be canonically ordered")
    for previous, current in zip(windows, windows[1:]):
        if previous.weekday == current.weekday and previous.end > current.start:
            raise ValueError("calendar windows overlap")
    canonical = json.dumps(
        data, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")
    if canonical != source_bytes:
        raise ValueError("calendar source requires canonical JSON bytes")
    return StaticCalendarSource(
        reference_id=data["reference_id"],
        reference_version=data["reference_version"],
        config_epoch=data["config_epoch"],
        timezone="UTC",
        windows=tuple(windows),
        source_sha256=sha256(source_bytes).hexdigest(),
    )


# This narrow static provenance root is reviewed/pinned within the shipped
# source, not accepted from a caller-supplied registry or runtime string.
# A different calendar needs a separately reviewed/pinned source and schema.
_BUNDLED_DOCUMENT = {
    "schema_version": 1,
    "reference_id": "sose:calendar:weekday-utc-v1",
    "reference_version": 1,
    "config_epoch": 0,
    "timezone": "UTC",
    "windows": [
        {"weekday": day, "start": "09:00", "end": "17:00"}
        for day in range(5)
    ],
}
BUNDLED_UTC_CALENDAR_BYTES = json.dumps(
    _BUNDLED_DOCUMENT, sort_keys=True, separators=(",", ":"),
    ensure_ascii=False, allow_nan=False,
).encode("utf-8")
# Pinned literal is deliberately independent from computed bytes.
BUNDLED_UTC_CALENDAR_SHA256 = (
    "c85fd9442be1913a652c245118484a1d272f7212c6cd8e2d15530f013c81e6e4"
)


def verify_static_calendar_reference(
    definition: OrganizationDefinition,
    reference: OperationalConfigReference,
) -> StaticCalendarSource:
    """Prove a declared reference matches *shipped pinned bytes*, not itself.

    There is no generic caller-register API. Matching this static source
    means only that its calendar *content* is known. It cannot grant
    availability, book capacity, authorize a job or change an epoch.
    """
    if not isinstance(definition, OrganizationDefinition):
        raise TypeError("calendar reference requires OrganizationDefinition")
    if not isinstance(reference, OperationalConfigReference):
        raise TypeError("calendar reference requires OperationalConfigReference")
    if reference.kind != "calendar":
        raise ValueError("unsupported non-calendar operational reference")
    if reference not in definition.operational_configs:
        raise ValueError("calendar reference is not declared by this organization")
    if reference.component_id not in {item.component_id for item in definition.components}:
        raise ValueError("calendar references another component")
    if sha256(BUNDLED_UTC_CALENDAR_BYTES).hexdigest() != BUNDLED_UTC_CALENDAR_SHA256:
        raise ValueError("shipped calendar provenance contradicts pinned source digest")
    source = parse_static_calendar_source(BUNDLED_UTC_CALENDAR_BYTES)
    if (
        reference.reference_id != source.reference_id
        or reference.reference_version != source.reference_version
        or reference.config_epoch != source.config_epoch
        or definition.config_epoch != source.config_epoch
        or reference.source_sha256 != source.source_sha256
    ):
        raise ValueError("calendar reference/provenance is not pinned to reviewed source")
    return source

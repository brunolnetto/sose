from __future__ import annotations

import re

from sose.persistence.codec import dumps
from sose.sinks.model import AnalyticalBatch


_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def validate_table_name(value: str) -> str:
    if not value:
        raise ValueError("warehouse table name cannot be empty")
    parts = value.split(".")
    if not all(_IDENTIFIER.fullmatch(part) for part in parts):
        raise ValueError(
            "warehouse table names must be dot-separated SQL-safe identifiers"
        )
    return value


def event_values(batch: AnalyticalBatch, event) -> tuple[object, ...]:
    return (
        event.event_id,
        batch.batch_id,
        batch.job_id,
        batch.domain_name,
        batch.config_revision,
        batch.logical_tick,
        batch.logical_time,
        event.name,
        event.entity_type,
        event.entity_id,
        event.occurred_at,
        event.tick,
        dumps(dict(event.payload)),
        event.causation_id,
        event.correlation_id,
    )


def batch_values(batch: AnalyticalBatch) -> tuple[object, ...]:
    return (
        batch.batch_id,
        batch.job_id,
        batch.domain_name,
        batch.config_revision,
        batch.logical_tick,
        batch.logical_time,
        len(batch.events),
    )

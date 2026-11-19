"""Energy meter-reading validation and ingestion (split from simulation.py)."""

from __future__ import annotations

import math
from datetime import datetime
from typing import TYPE_CHECKING

from sose.core.engine import Engine
from sose.persistence.memory import MemoryPersistence

from .entities import (
    Meter,
    MeterReading,
)
from .simulation_runtime import (
    _dispatch,
    _entity,
    _meter,
    flow_correlation_id,
    reading_id,
)

if TYPE_CHECKING:
    from .simulation import EnergyEntities


def _validate_meter_reading_inputs(
    *,
    quantity_kwh: float,
    quality: str,
    correction_ordinal: int,
    supersedes_reading_id: str | None,
) -> None:
    if not math.isfinite(float(quantity_kwh)) or quantity_kwh < 0:
        raise ValueError("meter reading quantity must be finite and non-negative")
    if quality not in {"actual", "estimated", "corrected"}:
        raise ValueError(f"unsupported meter reading quality: {quality}")
    if correction_ordinal < 0:
        raise ValueError("correction_ordinal must be non-negative")
    _validate_meter_reading_lineage(
        quality=quality,
        correction_ordinal=correction_ordinal,
        supersedes_reading_id=supersedes_reading_id,
    )


def _validate_meter_reading_lineage(
    *,
    quality: str,
    correction_ordinal: int,
    supersedes_reading_id: str | None,
) -> None:
    is_base = correction_ordinal == 0
    if is_base:
        _validate_base_meter_reading(
            quality=quality,
            supersedes_reading_id=supersedes_reading_id,
        )
        return
    _validate_correction_meter_reading(
        quality=quality,
        supersedes_reading_id=supersedes_reading_id,
    )


def _validate_base_meter_reading(
    *,
    quality: str,
    supersedes_reading_id: str | None,
) -> None:
    if supersedes_reading_id is not None:
        raise ValueError("base reading cannot supersede another reading")
    if quality == "corrected":
        raise ValueError("corrected quality requires correction lineage")


def _validate_correction_meter_reading(
    *,
    quality: str,
    supersedes_reading_id: str | None,
) -> None:
    if supersedes_reading_id is None:
        raise ValueError("corrected reading requires supersedes_reading_id")
    if quality != "corrected":
        raise ValueError("correction lineage requires corrected quality")


def _validate_existing_meter_reading(
    persistence: MemoryPersistence,
    engine: Engine,
    *,
    entities: EnergyEntities,
    reading_id_value: str,
    quantity_kwh: float,
    quality: str,
    supersedes_reading_id: str | None,
) -> MeterReading | None:
    existing = persistence.entity("utility_meter_reading", reading_id_value)
    if existing is None:
        return None
    if (
        float(existing.attributes["quantity_kwh"]) != float(quantity_kwh)
        or existing.attributes["quality"] != quality
        or existing.attributes.get("supersedes_reading_id") != supersedes_reading_id
    ):
        raise ValueError("meter reading identity already exists with different measurement")
    if existing.state == "captured":
        _dispatch(
            engine,
            existing,
            "commit",
            key=("energy-reading", existing.id, "commit"),
            correlation_id=flow_correlation_id(entities.service_point_id),
        )
        return _entity(persistence, "utility_meter_reading", existing.id)
    return existing


def _validate_superseded_reading(
    persistence: MemoryPersistence,
    *,
    meter: Meter,
    supersedes_reading_id: str | None,
    interval_end: datetime,
) -> None:
    if supersedes_reading_id is None:
        return
    superseded = _entity(
        persistence,
        "utility_meter_reading",
        supersedes_reading_id,
    )
    if superseded.state != "committed":
        raise RuntimeError("reading correction requires committed prior reading")
    if superseded.attributes["meter_id"] != meter.id:
        raise ValueError("reading correction must supersede the same meter")
    if superseded.attributes["interval_end"] != interval_end.isoformat():
        raise ValueError("reading correction must preserve interval_end")


def record_meter_reading(
    persistence: MemoryPersistence,
    engine: Engine,
    *,
    entities: EnergyEntities,
    interval_end: datetime,
    quantity_kwh: float,
    quality: str = "actual",
    correction_ordinal: int = 0,
    supersedes_reading_id: str | None = None,
) -> MeterReading | None:
    _validate_meter_reading_inputs(
        quantity_kwh=quantity_kwh,
        quality=quality,
        correction_ordinal=correction_ordinal,
        supersedes_reading_id=supersedes_reading_id,
    )
    rid = reading_id(
        entities.meter_id,
        interval_end,
        correction_ordinal=correction_ordinal,
    )
    existing = _validate_existing_meter_reading(
        persistence,
        engine,
        entities=entities,
        reading_id_value=rid,
        quantity_kwh=quantity_kwh,
        quality=quality,
        supersedes_reading_id=supersedes_reading_id,
    )
    if existing is not None:
        return existing

    if not engine.context.scenarios.attribute("energy.metering.available", True):
        return None

    meter = _meter(persistence, entities)
    if meter.state != "active":
        raise RuntimeError("meter reading requires active meter")
    _validate_superseded_reading(
        persistence,
        meter=meter,
        supersedes_reading_id=supersedes_reading_id,
        interval_end=interval_end,
    )

    reading = engine.context.entities.create(
        MeterReading,
        key=(
            "energy-reference",
            meter.id,
            interval_end.isoformat(),
            correction_ordinal,
        ),
        state="captured",
        attributes={
            "meter_id": meter.id,
            "service_point_id": entities.service_point_id,
            "interval_end": interval_end.isoformat(),
            "quantity_kwh": float(quantity_kwh),
            "quality": quality,
            "correction_ordinal": correction_ordinal,
            "supersedes_reading_id": supersedes_reading_id,
        },
    )
    with persistence.transaction() as uow:
        uow.save_entity(reading)
    _dispatch(
        engine,
        reading,
        "commit",
        key=("energy-reading", reading.id, "commit"),
        correlation_id=flow_correlation_id(entities.service_point_id),
    )
    return _entity(persistence, "utility_meter_reading", reading.id)

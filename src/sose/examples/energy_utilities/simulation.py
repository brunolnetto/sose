from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from sose.backends.simpy import SimPyBackend
from sose.core.clock import SimulationClock
from sose.core.context import SimulationContext
from sose.core.engine import Engine
from sose.core.identity import deterministic_id
from sose.core.randomness import RandomSource
from sose.core.scheduler import Scheduler
from sose.domain.registry import DomainRegistry, EntityType
from sose.persistence.memory import MemoryPersistence

from .entities import (
    DemandResponseEvent,
    DemandResponseParticipation,
    Meter,
    MeterReading,
    Outage,
    ServicePoint,
)
from .scenarios import ORIGIN
from .statecharts import (
    DemandResponseEventChart,
    DemandResponseParticipationChart,
    MeterChart,
    MeterReadingChart,
    OutageChart,
    ServicePointChart,
)


DR_START_DELAY = timedelta(hours=1)
DR_DURATION = timedelta(hours=3)


@dataclass(frozen=True, slots=True)
class EnergyEntities:
    service_point_id: str
    meter_id: str
    secondary_service_point_id: str | None = None
    secondary_meter_id: str | None = None


def flow_correlation_id(service_point_id: str) -> str:
    return deterministic_id("energy-utilities-flow", service_point_id)


def reading_id(
    meter_id: str,
    interval_end: datetime,
    *,
    correction_ordinal: int = 0,
) -> str:
    return deterministic_id(
        "entity",
        "utility_meter_reading",
        "energy-reference",
        meter_id,
        interval_end.isoformat(),
        correction_ordinal,
    )


def outage_id(service_point_id: str, incident_key: str) -> str:
    return deterministic_id(
        "entity",
        "utility_outage",
        "energy-reference",
        service_point_id,
        "outage",
        incident_key,
    )


def dr_event_id(event_key: str) -> str:
    return deterministic_id(
        "entity",
        "utility_dr_event",
        "energy-reference",
        "dr-event",
        event_key,
    )


def dr_participation_id(event_id: str, service_point_id: str) -> str:
    return deterministic_id(
        "entity",
        "utility_dr_participation",
        "energy-reference",
        event_id,
        service_point_id,
    )


def build_runtime(
    persistence: MemoryPersistence,
    *,
    now: datetime = ORIGIN,
    tick: int = 0,
    scenarios=(),
) -> tuple[SimulationContext, Engine]:
    context = SimulationContext(
        clock=SimulationClock(now=now, step=timedelta(hours=1), tick=tick),
        random=RandomSource(root_seed=1009),
        scheduler=Scheduler(),
    )
    registry = DomainRegistry()
    registry.register(EntityType("utility_service_point", ServicePointChart))
    registry.register(EntityType("utility_meter", MeterChart))
    registry.register(EntityType("utility_meter_reading", MeterReadingChart))
    registry.register(EntityType("utility_outage", OutageChart))
    registry.register(EntityType("utility_dr_event", DemandResponseEventChart))
    registry.register(
        EntityType(
            "utility_dr_participation",
            DemandResponseParticipationChart,
        )
    )
    return context, Engine(
        context=context,
        registry=registry,
        persistence=persistence,
        scenarios=scenarios,
    )


def seed_reference(persistence: MemoryPersistence) -> EnergyEntities:
    context, _ = build_runtime(persistence)

    def create_point(ordinal: int) -> tuple[ServicePoint, Meter]:
        service_point = context.entities.create(
            ServicePoint,
            key=("energy-reference", f"service-point-{ordinal}"),
            state="energized",
            attributes={
                "customer_id": f"customer-{ordinal}",
                "premise_id": f"premise-{ordinal}",
                "open_outage_keys": [],
            },
        )
        meter = context.entities.create(
            Meter,
            key=("energy-reference", service_point.id, f"meter-{ordinal}"),
            state="active",
            attributes={
                "service_point_id": service_point.id,
                "serial_number": f"MTR-SOSE-{ordinal:03d}",
                "quantity_kind": "energy",
                "unit": "kWh",
            },
        )
        service_point.attributes["meter_id"] = meter.id
        return service_point, meter

    primary_point, primary_meter = create_point(1)
    secondary_point, secondary_meter = create_point(2)
    with persistence.transaction() as uow:
        for entity in (
            primary_point,
            primary_meter,
            secondary_point,
            secondary_meter,
        ):
            uow.save_entity(entity)
    return EnergyEntities(
        service_point_id=primary_point.id,
        meter_id=primary_meter.id,
        secondary_service_point_id=secondary_point.id,
        secondary_meter_id=secondary_meter.id,
    )


def _entity(persistence, entity_type: str, entity_id: str):
    value = persistence.entity(entity_type, entity_id)
    if value is None:
        raise RuntimeError(f"{entity_type} was not persisted: {entity_id}")
    return value


def _service_point(
    persistence: MemoryPersistence,
    entities: EnergyEntities,
) -> ServicePoint:
    return _entity(
        persistence,
        "utility_service_point",
        entities.service_point_id,
    )


def _meter(persistence: MemoryPersistence, entities: EnergyEntities) -> Meter:
    return _entity(persistence, "utility_meter", entities.meter_id)


def _dispatch(
    engine: Engine,
    entity,
    event: str,
    *,
    key: tuple[object, ...],
    correlation_id: str,
) -> None:
    command = engine.context.commands.create(
        event,
        target=entity,
        correlation_id=correlation_id,
        key=key,
    )
    engine.dispatch(command)


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
    if quantity_kwh < 0:
        raise ValueError("meter reading quantity must be non-negative")
    if quality not in {"actual", "estimated", "corrected"}:
        raise ValueError(f"unsupported meter reading quality: {quality}")
    if correction_ordinal < 0:
        raise ValueError("correction_ordinal must be non-negative")
    if correction_ordinal == 0:
        if supersedes_reading_id is not None:
            raise ValueError("base reading cannot supersede another reading")
        if quality == "corrected":
            raise ValueError("corrected quality requires correction lineage")
    else:
        if supersedes_reading_id is None:
            raise ValueError("corrected reading requires supersedes_reading_id")
        if quality != "corrected":
            raise ValueError("correction lineage requires corrected quality")
    if not engine.context.scenarios.attribute("energy.metering.available", True):
        return None

    meter = _meter(persistence, entities)
    if meter.state != "active":
        raise RuntimeError("meter reading requires active meter")

    rid = reading_id(
        meter.id,
        interval_end,
        correction_ordinal=correction_ordinal,
    )
    existing = persistence.entity("utility_meter_reading", rid)
    if existing is not None:
        if (
            float(existing.attributes["quantity_kwh"]) != float(quantity_kwh)
            or existing.attributes["quality"] != quality
            or existing.attributes.get("supersedes_reading_id")
            != supersedes_reading_id
        ):
            raise ValueError(
                "meter reading identity already exists with different measurement"
            )
        if existing.state == "captured":
            _dispatch(
                engine,
                existing,
                "commit",
                key=("energy-reading", existing.id, "commit"),
                correlation_id=flow_correlation_id(entities.service_point_id),
            )
            existing = _entity(
                persistence,
                "utility_meter_reading",
                existing.id,
            )
        return existing

    if supersedes_reading_id is not None:
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


def report_outage(
    persistence: MemoryPersistence,
    engine: Engine,
    *,
    entities: EnergyEntities,
    incident_key: str,
    cause: str = "distribution_fault",
) -> Outage:
    if not incident_key:
        raise ValueError("incident_key must be non-empty")
    service_point = _service_point(persistence, entities)
    oid = outage_id(service_point.id, incident_key)
    outage = persistence.entity("utility_outage", oid)
    correlation_id = flow_correlation_id(service_point.id)

    if outage is None:
        outage = engine.context.entities.create(
            Outage,
            key=("energy-reference", service_point.id, "outage", incident_key),
            state="reported",
            attributes={
                "service_point_id": service_point.id,
                "incident_key": incident_key,
                "cause": cause,
            },
        )
        with persistence.transaction() as uow:
            uow.save_entity(outage)

    outage = _entity(persistence, "utility_outage", outage.id)
    if outage.state == "reported":
        _dispatch(
            engine,
            outage,
            "confirm",
            key=("energy-outage", outage.id, "confirm"),
            correlation_id=correlation_id,
        )
        outage = _entity(persistence, "utility_outage", outage.id)

    unresolved = outage.state != "restored"
    service_point = _service_point(persistence, entities)
    open_keys = list(service_point.attributes.get("open_outage_keys", []))
    if unresolved and incident_key not in open_keys:
        open_keys.append(incident_key)
        service_point.attributes["open_outage_keys"] = open_keys
        with persistence.transaction() as uow:
            uow.save_entity(service_point)
    service_point = _service_point(persistence, entities)
    if unresolved and service_point.state == "energized":
        _dispatch(
            engine,
            service_point,
            "interrupt",
            key=("energy-service-point", service_point.id, incident_key, "interrupt"),
            correlation_id=correlation_id,
        )
    return _entity(persistence, "utility_outage", outage.id)


def restore_outage(
    persistence: MemoryPersistence,
    engine: Engine,
    *,
    entities: EnergyEntities,
    incident_key: str,
) -> bool:
    service_point = _service_point(persistence, entities)
    outage = _entity(
        persistence,
        "utility_outage",
        outage_id(service_point.id, incident_key),
    )
    correlation_id = flow_correlation_id(service_point.id)

    if outage.state == "confirmed":
        _dispatch(
            engine,
            outage,
            "begin_restoration",
            key=("energy-outage", outage.id, "begin-restoration"),
            correlation_id=correlation_id,
        )
        outage = _entity(persistence, "utility_outage", outage.id)
    if outage.state == "restoring":
        _dispatch(
            engine,
            outage,
            "restore",
            key=("energy-outage", outage.id, "restore"),
            correlation_id=correlation_id,
        )
        outage = _entity(persistence, "utility_outage", outage.id)

    service_point = _service_point(persistence, entities)
    if outage.state == "restored":
        open_keys = [
            key
            for key in service_point.attributes.get("open_outage_keys", [])
            if key != incident_key
        ]
        if open_keys != service_point.attributes.get("open_outage_keys", []):
            service_point.attributes["open_outage_keys"] = open_keys
            with persistence.transaction() as uow:
                uow.save_entity(service_point)
            service_point = _service_point(persistence, entities)

    if (
        service_point.state == "interrupted"
        and not service_point.attributes.get("open_outage_keys", [])
    ):
        _dispatch(
            engine,
            service_point,
            "restore",
            key=("energy-service-point", service_point.id, incident_key, "restore"),
            correlation_id=correlation_id,
        )
    return outage.state == "restored"


def schedule_demand_response(
    persistence: MemoryPersistence,
    engine: Engine,
    backend: SimPyBackend,
    *,
    entities: EnergyEntities,
    event_key: str,
    start_delay: timedelta = DR_START_DELAY,
    duration: timedelta = DR_DURATION,
) -> tuple[DemandResponseEvent, DemandResponseParticipation, datetime, datetime]:
    if not event_key:
        raise ValueError("event_key must be non-empty")
    if start_delay < timedelta(0):
        raise ValueError("start_delay must be non-negative")
    if duration <= timedelta(0):
        raise ValueError("duration must be positive")

    eid = dr_event_id(event_key)
    event = persistence.entity("utility_dr_event", eid)
    participation = None
    if event is None:
        start_at = backend.now + start_delay
        end_at = start_at + duration
        event = engine.context.entities.create(
            DemandResponseEvent,
            key=("energy-reference", "dr-event", event_key),
            state="scheduled",
            attributes={
                "event_key": event_key,
                "service_point_id": entities.service_point_id,
                "start_at": start_at.isoformat(),
                "end_at": end_at.isoformat(),
            },
        )
        participation = engine.context.entities.create(
            DemandResponseParticipation,
            key=("energy-reference", event.id, entities.service_point_id),
            state="eligible",
            attributes={
                "event_id": event.id,
                "service_point_id": entities.service_point_id,
            },
        )
        with persistence.transaction() as uow:
            uow.save_entity(event)
            uow.save_entity(participation)
    else:
        start_at = datetime.fromisoformat(str(event.attributes["start_at"]))
        end_at = datetime.fromisoformat(str(event.attributes["end_at"]))
        participation = _entity(
            persistence,
            "utility_dr_participation",
            dr_participation_id(event.id, entities.service_point_id),
        )

    correlation_id = flow_correlation_id(entities.service_point_id)

    if event.state == "scheduled" and engine.scheduler.find_pending(
        entity_type="utility_dr_event",
        entity_id=event.id,
        name="start",
    ) is None:
        start_command = engine.context.commands.create(
            "start",
            target=event,
            due_at=start_at,
            correlation_id=correlation_id,
            key=("energy-dr", event.id, "start"),
        )
        engine.context.schedules.at(start_at, command=start_command)

    if event.state in {"scheduled", "active"} and engine.scheduler.find_pending(
        entity_type="utility_dr_event",
        entity_id=event.id,
        name="finish",
    ) is None:
        finish_command = engine.context.commands.create(
            "finish",
            target=event,
            due_at=end_at,
            correlation_id=correlation_id,
            key=("energy-dr", event.id, "finish"),
        )
        engine.context.schedules.at(end_at, command=finish_command)

    return (
        _entity(persistence, "utility_dr_event", event.id),
        _entity(persistence, "utility_dr_participation", participation.id),
        start_at,
        end_at,
    )


def cancel_demand_response(
    persistence: MemoryPersistence,
    engine: Engine,
    *,
    entities: EnergyEntities,
    event_key: str,
) -> bool:
    event = _entity(persistence, "utility_dr_event", dr_event_id(event_key))
    if event.state == "cancelled":
        return True
    if event.state == "completed":
        return False

    engine.scheduler.cancel_pending(
        entity_type="utility_dr_event",
        entity_id=event.id,
        name="start",
    )
    engine.scheduler.cancel_pending(
        entity_type="utility_dr_event",
        entity_id=event.id,
        name="finish",
    )
    _dispatch(
        engine,
        event,
        "cancel",
        key=("energy-dr", event.id, "cancel"),
        correlation_id=flow_correlation_id(entities.service_point_id),
    )
    return True


def opt_out_demand_response(
    persistence: MemoryPersistence,
    engine: Engine,
    *,
    entities: EnergyEntities,
    event_key: str,
) -> bool:
    event = _entity(persistence, "utility_dr_event", dr_event_id(event_key))
    participation = _entity(
        persistence,
        "utility_dr_participation",
        dr_participation_id(event.id, entities.service_point_id),
    )
    if participation.state in {"eligible", "active"}:
        _dispatch(
            engine,
            participation,
            "opt_out",
            key=("energy-dr-participation", participation.id, "opt-out"),
            correlation_id=flow_correlation_id(entities.service_point_id),
        )
    return True


def reconcile_demand_response(
    persistence: MemoryPersistence,
    engine: Engine,
    *,
    entities: EnergyEntities,
    event_key: str,
) -> bool:
    event = _entity(persistence, "utility_dr_event", dr_event_id(event_key))
    participation = _entity(
        persistence,
        "utility_dr_participation",
        dr_participation_id(event.id, entities.service_point_id),
    )
    service_point = _service_point(persistence, entities)
    correlation_id = flow_correlation_id(entities.service_point_id)

    if event.state == "active" and participation.state == "eligible":
        if service_point.state != "energized":
            return False
        if not engine.context.scenarios.attribute("energy.dr.available", True):
            return False
        _dispatch(
            engine,
            participation,
            "begin",
            key=("energy-dr-participation", participation.id, "begin"),
            correlation_id=correlation_id,
        )
        return True

    if event.state == "completed":
        if participation.state == "active":
            _dispatch(
                engine,
                participation,
                "complete",
                key=("energy-dr-participation", participation.id, "complete"),
                correlation_id=correlation_id,
            )
        elif participation.state == "eligible":
            _dispatch(
                engine,
                participation,
                "miss",
                key=("energy-dr-participation", participation.id, "miss"),
                correlation_id=correlation_id,
            )
        return True

    return participation.state in {"active", "completed", "missed", "opted_out"}


def run_happy_path() -> tuple[MemoryPersistence, EnergyEntities]:
    persistence = MemoryPersistence()
    entities = seed_reference(persistence)
    _, engine = build_runtime(persistence)
    backend = SimPyBackend(origin=ORIGIN)
    engine.rebuild_backend(backend)

    reading = record_meter_reading(
        persistence,
        engine,
        entities=entities,
        interval_end=ORIGIN,
        quantity_kwh=12.5,
    )
    if reading is None:
        raise RuntimeError("meter reading ingestion unavailable")

    _, _, start_at, end_at = schedule_demand_response(
        persistence,
        engine,
        backend,
        entities=entities,
        event_key="dr-1",
    )
    backend.run_until(start_at)
    if not reconcile_demand_response(
        persistence,
        engine,
        entities=entities,
        event_key="dr-1",
    ):
        raise RuntimeError("demand response participation failed to begin")
    backend.run_until(end_at)
    reconcile_demand_response(
        persistence,
        engine,
        entities=entities,
        event_key="dr-1",
    )
    return persistence, entities

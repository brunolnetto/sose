from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
import math

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
    step: timedelta = timedelta(hours=1),
    random_seed: int = 1009,
) -> tuple[SimulationContext, Engine]:
    context = SimulationContext(
        clock=SimulationClock(now=now, step=step, tick=tick),
        random=RandomSource(root_seed=random_seed),
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


def seed_reference(
    persistence: MemoryPersistence,
    *,
    now: datetime = ORIGIN,
    primary_customer_id: str = "customer-1",
    secondary_customer_id: str = "customer-2",
    include_secondary: bool = True,
    quantity_kind: str = "energy",
    unit: str = "kWh",
) -> EnergyEntities:
    context, _ = build_runtime(persistence, now=now)

    def create_point(
        ordinal: int,
        customer_id: str,
    ) -> tuple[ServicePoint, Meter]:
        service_point = context.entities.create(
            ServicePoint,
            key=("energy-reference", f"service-point-{ordinal}"),
            state="energized",
            attributes={
                "customer_id": customer_id,
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
                "quantity_kind": quantity_kind,
                "unit": unit,
            },
        )
        service_point.attributes["meter_id"] = meter.id
        return service_point, meter

    primary_point, primary_meter = create_point(1, primary_customer_id)
    secondary = (
        create_point(2, secondary_customer_id)
        if include_secondary
        else None
    )
    with persistence.transaction() as uow:
        uow.save_entity(primary_point)
        uow.save_entity(primary_meter)
        if secondary is not None:
            secondary_point, secondary_meter = secondary
            uow.save_entity(secondary_point)
            uow.save_entity(secondary_meter)
    return EnergyEntities(
        service_point_id=primary_point.id,
        meter_id=primary_meter.id,
        secondary_service_point_id=(
            None if secondary is None else secondary[0].id
        ),
        secondary_meter_id=(
            None if secondary is None else secondary[1].id
        ),
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
    target_service_point_ids: tuple[str, ...] | None = None,
) -> tuple[DemandResponseEvent, DemandResponseParticipation, datetime, datetime]:
    _validate_demand_response_schedule_inputs(
        event_key=event_key,
        start_delay=start_delay,
        duration=duration,
    )
    requested_targets = _resolve_demand_response_targets(
        persistence,
        entities=entities,
        target_service_point_ids=target_service_point_ids,
    )
    event, participation, start_at, end_at = _load_or_create_demand_response_event(
        persistence,
        engine,
        backend,
        entities=entities,
        event_key=event_key,
        requested_targets=requested_targets,
        start_delay=start_delay,
        duration=duration,
        target_service_point_ids=target_service_point_ids,
    )

    correlation_id = flow_correlation_id(entities.service_point_id)
    _schedule_event_command_if_missing(
        engine,
        event=event,
        name="start",
        allowed_states={"scheduled"},
        due_at=start_at,
        correlation_id=correlation_id,
        key=("energy-dr", event.id, "start"),
    )
    _schedule_event_command_if_missing(
        engine,
        event=event,
        name="finish",
        allowed_states={"scheduled", "active"},
        due_at=end_at,
        correlation_id=correlation_id,
        key=("energy-dr", event.id, "finish"),
    )

    return (
        _entity(persistence, "utility_dr_event", event.id),
        _entity(persistence, "utility_dr_participation", participation.id),
        start_at,
        end_at,
    )


def _validate_demand_response_schedule_inputs(
    *,
    event_key: str,
    start_delay: timedelta,
    duration: timedelta,
) -> None:
    if not event_key:
        raise ValueError("event_key must be non-empty")
    if start_delay < timedelta(0):
        raise ValueError("start_delay must be non-negative")
    if duration <= timedelta(0):
        raise ValueError("duration must be positive")


def _resolve_demand_response_targets(
    persistence: MemoryPersistence,
    *,
    entities: EnergyEntities,
    target_service_point_ids: tuple[str, ...] | None,
) -> tuple[str, ...]:
    requested_targets = tuple(
        dict.fromkeys(target_service_point_ids or (entities.service_point_id,))
    )
    if entities.service_point_id not in requested_targets:
        raise ValueError("primary service point must belong to target population")
    for service_point_id in requested_targets:
        _entity(persistence, "utility_service_point", service_point_id)
    return requested_targets


def _create_demand_response_event(
    persistence: MemoryPersistence,
    engine: Engine,
    *,
    entities: EnergyEntities,
    event_key: str,
    requested_targets: tuple[str, ...],
    start_at: datetime,
    end_at: datetime,
) -> tuple[DemandResponseEvent, DemandResponseParticipation]:
    event = engine.context.entities.create(
        DemandResponseEvent,
        key=("energy-reference", "dr-event", event_key),
        state="scheduled",
        attributes={
            "event_key": event_key,
            "service_point_id": entities.service_point_id,
            "target_service_point_ids": list(requested_targets),
            "start_at": start_at.isoformat(),
            "end_at": end_at.isoformat(),
        },
    )
    participations = [
        engine.context.entities.create(
            DemandResponseParticipation,
            key=("energy-reference", event.id, service_point_id),
            state="eligible",
            attributes={
                "event_id": event.id,
                "service_point_id": service_point_id,
            },
        )
        for service_point_id in requested_targets
    ]
    participation = next(
        value
        for value in participations
        if value.attributes["service_point_id"] == entities.service_point_id
    )
    with persistence.transaction() as uow:
        uow.save_entity(event)
        for value in participations:
            uow.save_entity(value)
    return event, participation


def _load_or_create_demand_response_event(
    persistence: MemoryPersistence,
    engine: Engine,
    backend: SimPyBackend,
    *,
    entities: EnergyEntities,
    event_key: str,
    requested_targets: tuple[str, ...],
    start_delay: timedelta,
    duration: timedelta,
    target_service_point_ids: tuple[str, ...] | None,
) -> tuple[DemandResponseEvent, DemandResponseParticipation, datetime, datetime]:
    event = persistence.entity("utility_dr_event", dr_event_id(event_key))
    if event is None:
        start_at = backend.now + start_delay
        end_at = start_at + duration
        event, participation = _create_demand_response_event(
            persistence,
            engine,
            entities=entities,
            event_key=event_key,
            requested_targets=requested_targets,
            start_at=start_at,
            end_at=end_at,
        )
        return event, participation, start_at, end_at

    start_at = datetime.fromisoformat(str(event.attributes["start_at"]))
    end_at = datetime.fromisoformat(str(event.attributes["end_at"]))
    persisted_targets = tuple(event.attributes.get("target_service_point_ids", []))
    if target_service_point_ids is not None and requested_targets != persisted_targets:
        raise ValueError("demand-response target population cannot change on replay")
    participation = _entity(
        persistence,
        "utility_dr_participation",
        dr_participation_id(event.id, entities.service_point_id),
    )
    return event, participation, start_at, end_at


def _schedule_event_command_if_missing(
    engine: Engine,
    *,
    event: DemandResponseEvent,
    name: str,
    allowed_states: set[str],
    due_at: datetime,
    correlation_id: str,
    key: tuple[object, ...],
) -> None:
    if event.state not in allowed_states:
        return
    if engine.scheduler.find_pending(
        entity_type="utility_dr_event",
        entity_id=event.id,
        name=name,
    ) is not None:
        return
    command = engine.context.commands.create(
        name,
        target=event,
        due_at=due_at,
        correlation_id=correlation_id,
        key=key,
    )
    engine.context.schedules.at(due_at, command=command)


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
    for service_point_id in event.attributes.get(
        "target_service_point_ids",
        [entities.service_point_id],
    ):
        participation = persistence.entity(
            "utility_dr_participation",
            dr_participation_id(event.id, str(service_point_id)),
        )
        if participation is not None and participation.state in {"eligible", "active"}:
            _dispatch(
                engine,
                participation,
                "opt_out",
                key=("energy-dr-participation", participation.id, "event-cancel"),
                correlation_id=flow_correlation_id(str(service_point_id)),
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
    target_ids = tuple(
        str(value)
        for value in event.attributes.get(
            "target_service_point_ids",
            [entities.service_point_id],
        )
    )
    available = bool(engine.context.scenarios.attribute("energy.dr.available", True))
    return all(
        _reconcile_participation_for_event_state(
            persistence,
            engine,
            event=event,
            service_point_id=service_point_id,
            available=available,
        )
        for service_point_id in target_ids
    )


def _reconcile_participation_for_event_state(
    persistence: MemoryPersistence,
    engine: Engine,
    *,
    event: DemandResponseEvent,
    service_point_id: str,
    available: bool,
) -> bool:
    participation = _entity(
        persistence,
        "utility_dr_participation",
        dr_participation_id(event.id, service_point_id),
    )
    correlation_id = flow_correlation_id(service_point_id)
    if event.state == "active":
        return _reconcile_active_participation(
            persistence,
            engine,
            service_point_id=service_point_id,
            participation=participation,
            available=available,
            correlation_id=correlation_id,
        )
    if event.state == "completed":
        _reconcile_completed_participation(
            engine,
            participation=participation,
            correlation_id=correlation_id,
        )
        return True
    return participation.state in {"active", "completed", "missed", "opted_out"}


def _reconcile_active_participation(
    persistence: MemoryPersistence,
    engine: Engine,
    *,
    service_point_id: str,
    participation: DemandResponseParticipation,
    available: bool,
    correlation_id: str,
) -> bool:
    if participation.state != "eligible":
        return True
    service_point = _entity(
        persistence,
        "utility_service_point",
        service_point_id,
    )
    if service_point.state != "energized" or not available:
        return False
    _dispatch(
        engine,
        participation,
        "begin",
        key=("energy-dr-participation", participation.id, "begin"),
        correlation_id=correlation_id,
    )
    return True


def _reconcile_completed_participation(
    engine: Engine,
    *,
    participation: DemandResponseParticipation,
    correlation_id: str,
) -> None:
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

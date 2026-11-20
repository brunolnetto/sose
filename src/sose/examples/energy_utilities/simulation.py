from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from sose.backends.simpy import SimPyBackend
from sose.core.engine import Engine
from sose.core.randomness import RandomSource  # noqa: F401
from sose.persistence.memory import MemoryPersistence

from .entities import Meter, Outage, ServicePoint
from .scenarios import ORIGIN
from .simulation_runtime import (
    flow_correlation_id,
    outage_id,
    dr_event_id,
    dr_participation_id,
    build_runtime,
    _entity,
    _service_point,
    _dispatch,
)
from .simulation_metering import record_meter_reading
from .simulation_demand_response import schedule_demand_response, reconcile_demand_response


@dataclass(frozen=True, slots=True)
class EnergyEntities:
    service_point_id: str
    meter_id: str
    secondary_service_point_id: str | None = None
    secondary_meter_id: str | None = None


def seed_reference(
    persistence: MemoryPersistence,
    *,
    owner_id: str,
    now: datetime = ORIGIN,
    primary_customer_id: str = "customer-1",
    secondary_customer_id: str = "customer-2",
    include_secondary: bool = True,
    quantity_kind: str = "energy",
    unit: str = "kWh",
) -> EnergyEntities:
    context, _ = build_runtime(persistence, owner_id=owner_id, now=now)

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


def run_happy_path(*, owner_id: str) -> tuple[MemoryPersistence, EnergyEntities]:
    persistence = MemoryPersistence()
    entities = seed_reference(persistence, owner_id=owner_id)
    _, engine = build_runtime(persistence, owner_id=owner_id)
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

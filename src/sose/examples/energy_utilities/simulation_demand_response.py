"""Energy demand-response scheduling, opt-out and reconciliation (split from simulation.py)."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import TYPE_CHECKING

from sose.backends.simpy import SimPyBackend
from sose.core.engine import Engine
from sose.persistence.memory import MemoryPersistence

from .entities import (
    DemandResponseEvent,
    DemandResponseParticipation,
)
from .simulation_runtime import (
    DR_DURATION,
    DR_START_DELAY,
    _dispatch,
    _entity,
    dr_event_id,
    dr_participation_id,
    flow_correlation_id,
)

if TYPE_CHECKING:
    from .simulation import EnergyEntities


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

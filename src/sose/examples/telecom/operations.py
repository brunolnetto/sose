"""Telecom usage metering and service-assurance incident workflow (split from simulation.py)."""

from __future__ import annotations

import math

from sose.core.engine import Engine
from sose.persistence.memory import MemoryPersistence

from .entities import (
    NetworkAlarm,
    TroubleTicket,
    UsageRecord,
)
from .foundation import (
    TelecomEntities,
    _entity,
    _service,
    alarm_id,
    flow_correlation_id,
    trouble_ticket_id,
    usage_record_id,
)


def _dispatch(*args, **kwargs):
    """Forward to ``simulation._dispatch`` at call time so patches on it still apply."""
    from . import simulation

    return simulation._dispatch(*args, **kwargs)


def record_usage(
    persistence: MemoryPersistence,
    engine: Engine,
    *,
    entities: TelecomEntities,
    sequence: int,
    quantity: float,
    unit: str = "MB",
) -> UsageRecord:
    if sequence <= 0:
        raise ValueError("sequence must be positive")
    if not math.isfinite(float(quantity)) or quantity <= 0:
        raise ValueError("quantity must be finite and positive")
    service = _service(persistence, entities.product_order_id)
    if service is None:
        raise RuntimeError("usage requires active subscription service")

    uid = usage_record_id(service.id, sequence)
    existing = persistence.entity("telecom_usage_record", uid)
    if existing is not None:
        if (
            float(existing.attributes["quantity"]) != float(quantity)
            or existing.attributes["unit"] != unit
        ):
            raise ValueError("usage identity already exists with different measurement")
        if existing.state == "captured":
            _dispatch(
                engine,
                existing,
                "commit",
                key=("telecom-usage", existing.id, "commit"),
                correlation_id=flow_correlation_id(entities.product_order_id),
            )
            existing = _entity(
                persistence,
                "telecom_usage_record",
                existing.id,
            )
        return existing

    if service.state != "active":
        raise RuntimeError("usage requires active subscription service")

    usage = engine.context.entities.create(
        UsageRecord,
        key=("telecom-reference", service.id, "usage", sequence),
        state="captured",
        attributes={
            "service_id": service.id,
            "sequence": sequence,
            "quantity": float(quantity),
            "unit": unit,
            "rated": False,
        },
    )
    with persistence.transaction() as uow:
        uow.save_entity(usage)
    _dispatch(
        engine,
        usage,
        "commit",
        key=("telecom-usage", usage.id, "commit"),
        correlation_id=flow_correlation_id(entities.product_order_id),
    )
    return _entity(persistence, "telecom_usage_record", usage.id)


def raise_service_alarm(
    persistence: MemoryPersistence,
    engine: Engine,
    *,
    entities: TelecomEntities,
    incident_key: str,
    severity: str = "major",
) -> tuple[NetworkAlarm, TroubleTicket]:
    if not incident_key:
        raise ValueError("incident_key must be non-empty")
    service = _service(persistence, entities.product_order_id)
    if service is None or service.state not in {"active", "suspended"}:
        raise RuntimeError("alarm requires provisioned subscription service")
    correlation_id = flow_correlation_id(entities.product_order_id)

    aid = alarm_id(service.id, incident_key)
    tid = trouble_ticket_id(service.id, incident_key)
    alarm, ticket = _load_or_create_alarm_and_ticket(
        persistence,
        engine,
        service_id=service.id,
        incident_key=incident_key,
        severity=severity,
        alarm_entity_id=aid,
        ticket_entity_id=tid,
    )
    unresolved = alarm.state != "cleared" or ticket.state != "closed"

    service = _entity(
        persistence,
        "telecom_subscription_service",
        service.id,
    )
    _append_open_incident_if_needed(
        persistence,
        service=service,
        incident_key=incident_key,
        unresolved=unresolved,
    )
    if unresolved and service.state == "active":
        _dispatch(
            engine,
            service,
            "suspend",
            key=("telecom-service", service.id, incident_key, "suspend"),
            correlation_id=correlation_id,
        )
    return (
        _entity(persistence, "telecom_network_alarm", alarm.id),
        _entity(persistence, "telecom_trouble_ticket", ticket.id),
    )


def acknowledge_incident(
    persistence: MemoryPersistence,
    engine: Engine,
    *,
    entities: TelecomEntities,
    incident_key: str,
) -> None:
    service = _service(persistence, entities.product_order_id)
    if service is None:
        raise RuntimeError("service was not provisioned")
    correlation_id = flow_correlation_id(entities.product_order_id)
    alarm = _entity(
        persistence,
        "telecom_network_alarm",
        alarm_id(service.id, incident_key),
    )
    ticket = _entity(
        persistence,
        "telecom_trouble_ticket",
        trouble_ticket_id(service.id, incident_key),
    )
    if alarm.state == "raised":
        _dispatch(
            engine,
            alarm,
            "acknowledge",
            key=("telecom-alarm", alarm.id, "acknowledge"),
            correlation_id=correlation_id,
        )
    if ticket.state == "open":
        _dispatch(
            engine,
            ticket,
            "acknowledge",
            key=("telecom-ticket", ticket.id, "acknowledge"),
            correlation_id=correlation_id,
        )


def restore_service(
    persistence: MemoryPersistence,
    engine: Engine,
    *,
    entities: TelecomEntities,
    incident_key: str,
) -> bool:
    service = _service(persistence, entities.product_order_id)
    if service is None:
        return False
    correlation_id = flow_correlation_id(entities.product_order_id)
    alarm = _entity(
        persistence,
        "telecom_network_alarm",
        alarm_id(service.id, incident_key),
    )
    ticket = _entity(
        persistence,
        "telecom_trouble_ticket",
        trouble_ticket_id(service.id, incident_key),
    )

    _clear_alarm_if_needed(
        engine,
        alarm=alarm,
        correlation_id=correlation_id,
    )
    _progress_ticket_resolution(
        persistence,
        engine,
        ticket=ticket,
        correlation_id=correlation_id,
    )
    alarm = _entity(persistence, "telecom_network_alarm", alarm.id)
    ticket = _entity(persistence, "telecom_trouble_ticket", ticket.id)
    service = _entity(
        persistence,
        "telecom_subscription_service",
        service.id,
    )
    service = _drop_open_incident_if_resolved(
        persistence,
        service=service,
        incident_key=incident_key,
        alarm=alarm,
        ticket=ticket,
    )

    if (
        service.state == "suspended"
        and not service.attributes.get("open_incident_keys", [])
    ):
        _dispatch(
            engine,
            service,
            "restore",
            key=("telecom-service", service.id, incident_key, "restore"),
            correlation_id=correlation_id,
        )
    return True


def _load_or_create_alarm_and_ticket(
    persistence: MemoryPersistence,
    engine: Engine,
    *,
    service_id: str,
    incident_key: str,
    severity: str,
    alarm_entity_id: str,
    ticket_entity_id: str,
) -> tuple[NetworkAlarm, TroubleTicket]:
    alarm = persistence.entity("telecom_network_alarm", alarm_entity_id)
    ticket = persistence.entity("telecom_trouble_ticket", ticket_entity_id)
    if alarm is None:
        alarm = engine.context.entities.create(
            NetworkAlarm,
            key=("telecom-reference", service_id, "alarm", incident_key),
            state="raised",
            attributes={
                "service_id": service_id,
                "incident_key": incident_key,
                "severity": severity,
            },
        )
    if ticket is None:
        ticket = engine.context.entities.create(
            TroubleTicket,
            key=("telecom-reference", service_id, "ticket", incident_key),
            state="open",
            attributes={
                "service_id": service_id,
                "alarm_id": alarm.id,
                "incident_key": incident_key,
                "severity": severity,
            },
        )
    with persistence.transaction() as uow:
        if persistence.entity("telecom_network_alarm", alarm.id) is None:
            uow.save_entity(alarm)
        if persistence.entity("telecom_trouble_ticket", ticket.id) is None:
            uow.save_entity(ticket)
    return (
        _entity(persistence, "telecom_network_alarm", alarm.id),
        _entity(persistence, "telecom_trouble_ticket", ticket.id),
    )


def _append_open_incident_if_needed(
    persistence: MemoryPersistence,
    *,
    service,
    incident_key: str,
    unresolved: bool,
) -> None:
    if not unresolved:
        return
    open_incidents = list(service.attributes.get("open_incident_keys", []))
    if incident_key in open_incidents:
        return
    open_incidents.append(incident_key)
    service.attributes["open_incident_keys"] = open_incidents
    with persistence.transaction() as uow:
        uow.save_entity(service)


def _clear_alarm_if_needed(
    engine: Engine,
    *,
    alarm,
    correlation_id: str,
) -> None:
    if alarm.state not in {"raised", "acknowledged"}:
        return
    _dispatch(
        engine,
        alarm,
        "clear",
        key=("telecom-alarm", alarm.id, "clear"),
        correlation_id=correlation_id,
    )


def _progress_ticket_resolution(
    persistence: MemoryPersistence,
    engine: Engine,
    *,
    ticket,
    correlation_id: str,
) -> None:
    ticket_state = _entity(persistence, "telecom_trouble_ticket", ticket.id)
    if ticket_state.state in {"open", "acknowledged"}:
        _dispatch(
            engine,
            ticket_state,
            "resolve",
            key=("telecom-ticket", ticket_state.id, "resolve"),
            correlation_id=correlation_id,
        )
        ticket_state = _entity(persistence, "telecom_trouble_ticket", ticket.id)
    if ticket_state.state == "resolved":
        _dispatch(
            engine,
            ticket_state,
            "close",
            key=("telecom-ticket", ticket_state.id, "close"),
            correlation_id=correlation_id,
        )


def _drop_open_incident_if_resolved(
    persistence: MemoryPersistence,
    *,
    service,
    incident_key: str,
    alarm,
    ticket,
):
    if alarm.state != "cleared" or ticket.state != "closed":
        return service
    open_incidents = [
        key
        for key in service.attributes.get("open_incident_keys", [])
        if key != incident_key
    ]
    if open_incidents == service.attributes.get("open_incident_keys", []):
        return service
    service.attributes["open_incident_keys"] = open_incidents
    with persistence.transaction() as uow:
        uow.save_entity(service)
    return _entity(
        persistence,
        "telecom_subscription_service",
        service.id,
    )

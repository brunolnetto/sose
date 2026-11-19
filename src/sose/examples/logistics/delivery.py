"""Logistics delivery attempts: dispatch, success and failure (split from simulation.py)."""

from __future__ import annotations

from datetime import datetime, timedelta

from sose.backends.simpy import SimPyBackend
from sose.core.engine import Engine
from sose.core.identity import deterministic_id
from sose.persistence.memory import MemoryPersistence

from .entities import DeliveryAttempt
from .queues import _ensure_destination_queue_dequeue
from .records import (
    RETRY_DELAY,
    LogisticsEntities,
    _attempt,
    _shipment,
    flow_correlation_id,
)


def _dispatch(engine: Engine, entity, event: str, *, key: tuple[object, ...]) -> None:
    """Dispatch via ``simulation._dispatch`` so patches on that module still apply."""
    from . import simulation

    simulation._dispatch(engine, entity, event, key=key)


def ensure_delivery_attempt(
    persistence: MemoryPersistence,
    engine: Engine,
    *,
    ordinal: int,
    shipment_id: str | None = None,
) -> DeliveryAttempt:
    reference_shipment = deterministic_id(
        "entity", "shipment", "logistics-reference", "shipment-1",
    )
    actual_shipment = reference_shipment if shipment_id is None else shipment_id
    existing = _attempt(persistence, ordinal, shipment_id=actual_shipment)
    if existing is not None:
        if existing.attributes.get("shipment_id") != actual_shipment:
            raise ValueError("delivery attempt belongs to another shipment")
        return existing

    attempt = engine.context.entities.create(
        DeliveryAttempt,
        key=("logistics-reference",
             "shipment-1" if actual_shipment == reference_shipment else actual_shipment,
             "attempt", ordinal),
        state="pending",
        attributes={"shipment_id": actual_shipment, "ordinal": ordinal},
    )
    with persistence.transaction() as uow:
        uow.save_entity(attempt)
    return attempt


def _resume_destination_hub_if_available(
    persistence: MemoryPersistence,
    engine: Engine,
    *,
    entities: LogisticsEntities,
    ordinal: int,
    courier_available: bool,
):
    shipment = _shipment(persistence, entities)
    if shipment.state != "delayed_destination_hub" or not courier_available:
        return shipment
    _dispatch(
        engine,
        shipment,
        "resume",
        key=("logistics-delivery", shipment.id, ordinal, "capacity-resume"),
    )
    return _shipment(persistence, entities)


def _eligible_delivery_dispatch_state(shipment) -> bool:
    return shipment.state in {"at_destination_hub", "out_for_delivery"}


def _attempt_ready_for_dispatch(attempt: DeliveryAttempt) -> bool:
    if attempt.state == "out_for_delivery":
        return True
    return attempt.state == "pending"


def _dispatch_delay_if_unavailable(
    engine: Engine,
    *,
    shipment,
    ordinal: int,
    courier_available: bool,
) -> bool:
    if courier_available:
        return False
    if shipment.state == "at_destination_hub":
        _dispatch(
            engine,
            shipment,
            "delay",
            key=("logistics-delivery", shipment.id, ordinal, "capacity-delay"),
        )
    return True


def _dispatch_shipment_for_delivery(
    persistence: MemoryPersistence,
    engine: Engine,
    backend: SimPyBackend,
    *,
    entities: LogisticsEntities,
    shipment,
    ordinal: int,
) -> bool:
    if shipment.state != "at_destination_hub":
        return True
    if not _ensure_destination_queue_dequeue(
        persistence,
        engine,
        backend,
        shipment_id=shipment.id,
    ):
        return False
    shipment = _shipment(persistence, entities)
    _dispatch(
        engine,
        shipment,
        "dispatch_delivery",
        key=("logistics-delivery", shipment.id, ordinal, "dispatch-shipment"),
    )
    return True


def reconcile_delivery_dispatch(
    persistence: MemoryPersistence,
    engine: Engine,
    backend: SimPyBackend,
    *,
    entities: LogisticsEntities,
    ordinal: int,
) -> bool:
    courier_available = engine.context.scenarios.attribute(
        "logistics.courier.available", True
    )
    shipment = _resume_destination_hub_if_available(
        persistence,
        engine,
        entities=entities,
        ordinal=ordinal,
        courier_available=courier_available,
    )
    if not _eligible_delivery_dispatch_state(shipment):
        return False

    attempt = ensure_delivery_attempt(
        persistence, engine, ordinal=ordinal, shipment_id=entities.shipment_id,
    )
    if attempt.state == "out_for_delivery":
        return True
    if not _attempt_ready_for_dispatch(attempt):
        return False

    if _dispatch_delay_if_unavailable(
        engine,
        shipment=shipment,
        ordinal=ordinal,
        courier_available=courier_available,
    ):
        return False

    request_id = f"delivery-courier:{attempt.id}"
    if engine.resources.ensure_requested(
        backend,
        resource_name="delivery_courier",
        request_id=request_id,
        requested_at=backend.now,
    ) is None:
        return False

    if not _dispatch_shipment_for_delivery(
        persistence,
        engine,
        backend,
        entities=entities,
        shipment=shipment,
        ordinal=ordinal,
    ):
        return False

    attempt = _attempt(persistence, ordinal, shipment_id=entities.shipment_id)
    if attempt is None:
        raise RuntimeError("delivery attempt disappeared")
    _dispatch(
        engine,
        attempt,
        "dispatch",
        key=("logistics-delivery", attempt.id, "dispatch"),
    )
    return True


def reconcile_delivery_success(
    persistence: MemoryPersistence,
    engine: Engine,
    backend: SimPyBackend,
    *,
    entities: LogisticsEntities,
    ordinal: int,
) -> None:
    attempt = _attempt(persistence, ordinal, shipment_id=entities.shipment_id)
    shipment = _shipment(persistence, entities)
    if attempt is None or attempt.state != "out_for_delivery":
        raise RuntimeError("delivery attempt is not active")
    if shipment.state != "out_for_delivery":
        raise RuntimeError("shipment is not out for delivery")

    _dispatch(
        engine,
        attempt,
        "deliver",
        key=("logistics-delivery", attempt.id, "deliver"),
    )
    shipment = _shipment(persistence, entities)
    _dispatch(
        engine,
        shipment,
        "deliver",
        key=("logistics-delivery", shipment.id, ordinal, "deliver"),
    )
    engine.resources.withdraw(backend, f"delivery-courier:{attempt.id}")


def reconcile_delivery_failure(
    persistence: MemoryPersistence,
    engine: Engine,
    backend: SimPyBackend,
    *,
    entities: LogisticsEntities,
    ordinal: int,
    retry_after: timedelta = RETRY_DELAY,
) -> datetime:
    attempt = _attempt(persistence, ordinal, shipment_id=entities.shipment_id)
    shipment = _shipment(persistence, entities)
    if attempt is None or attempt.state != "out_for_delivery":
        raise RuntimeError("delivery attempt is not active")
    if shipment.state != "out_for_delivery":
        raise RuntimeError("shipment is not out for delivery")

    _dispatch(
        engine,
        attempt,
        "fail",
        key=("logistics-delivery", attempt.id, "fail"),
    )
    shipment = _shipment(persistence, entities)
    _dispatch(
        engine,
        shipment,
        "delay",
        key=("logistics-delivery", shipment.id, ordinal, "retry-delay"),
    )

    shipment = _shipment(persistence, entities)
    due_at = backend.now + retry_after
    command = engine.context.commands.create(
        "resume",
        target=shipment,
        due_at=due_at,
        correlation_id=flow_correlation_id(),
        key=("logistics-delivery", shipment.id, ordinal, "retry-resume"),
    )
    engine.context.schedules.at(due_at, command=command)
    engine.resources.withdraw(backend, f"delivery-courier:{attempt.id}")
    return due_at

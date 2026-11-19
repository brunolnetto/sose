"""Finite-capacity fulfillment service-task lifecycle (split from simulation.py)."""

from __future__ import annotations

from datetime import datetime, timedelta

from sose.core.engine import Engine
from sose.persistence.memory import MemoryPersistence

from .entities import FulfillmentServiceTask
from .handling import pack_order, pick_allocation, ship_order
from .records import WarehouseEntities, _dispatch, _entity, flow_correlation_id, service_task_id


def _service_task_entity(
    persistence: MemoryPersistence,
    *,
    stage: str,
    subject_id: str,
) -> FulfillmentServiceTask | None:
    return persistence.entity(
        "warehouse_fulfillment_service_task",
        service_task_id(stage, subject_id),
    )


def _save_entity(persistence: MemoryPersistence, entity) -> None:
    with persistence.transaction() as uow:
        uow.save_entity(entity)


def _ensure_service_task(
    persistence: MemoryPersistence,
    engine: Engine,
    *,
    stage: str,
    subject_id: str,
    order_id: str,
    resource_name: str,
    service_duration: timedelta,
) -> FulfillmentServiceTask:
    if service_duration <= timedelta(0):
        raise ValueError("service duration must be positive")

    existing = _service_task_entity(
        persistence,
        stage=stage,
        subject_id=subject_id,
    )
    duration_seconds = service_duration.total_seconds()
    if existing is not None:
        if existing.attributes.get("resource_name") != resource_name:
            raise ValueError("service task resource identity changed")
        if float(existing.attributes.get("service_duration_seconds", 0.0)) != duration_seconds:
            raise ValueError("service task duration changed after creation")
        return existing

    task = engine.context.entities.create(
        FulfillmentServiceTask,
        key=("warehouse-reference", "service", stage, subject_id),
        state="queued",
        attributes={
            "stage": stage,
            "subject_id": subject_id,
            "order_id": order_id,
            "resource_name": resource_name,
            "request_id": f"warehouse-fulfillment:{stage}:{subject_id}",
            "requested_at": engine.context.clock.now.isoformat(),
            "acquired_at": None,
            "service_duration_seconds": duration_seconds,
            "completion_due_at": None,
            "reservation_id": None,
            "business_applied": False,
            "resource_released": False,
        },
    )
    _save_entity(persistence, task)
    return task


def _start_service_task_if_capacity_available(
    persistence: MemoryPersistence,
    engine: Engine,
    backend,
    *,
    task: FulfillmentServiceTask,
) -> FulfillmentServiceTask:
    if task.state == "completed":
        return task

    requested_at = datetime.fromisoformat(str(task.attributes["requested_at"]))
    reservation = engine.resources.ensure_requested(
        backend,
        resource_name=str(task.attributes["resource_name"]),
        request_id=str(task.attributes["request_id"]),
        requested_at=requested_at,
    )
    task = _entity(
        persistence,
        "warehouse_fulfillment_service_task",
        task.id,
    )
    if reservation is None:
        return task

    if task.attributes.get("acquired_at") is None:
        acquired_at = reservation.acquired_at
        duration = timedelta(
            seconds=float(task.attributes["service_duration_seconds"])
        )
        task.attributes["acquired_at"] = acquired_at.isoformat()
        task.attributes["completion_due_at"] = (acquired_at + duration).isoformat()
        task.attributes["reservation_id"] = reservation.reservation_id
        _save_entity(persistence, task)

    task = _entity(
        persistence,
        "warehouse_fulfillment_service_task",
        task.id,
    )
    if task.state == "queued":
        _dispatch(
            engine,
            task,
            "start",
            key=("warehouse-service", task.id, "start"),
        )
        task = _entity(
            persistence,
            "warehouse_fulfillment_service_task",
            task.id,
        )

    pending = engine.scheduler.find_pending(
        entity_type=task.entity_type,
        entity_id=task.id,
        name="complete",
    )
    if task.state == "in_progress" and pending is None:
        due_at = datetime.fromisoformat(str(task.attributes["completion_due_at"]))
        command = engine.context.commands.create(
            "complete",
            target=task,
            due_at=due_at,
            correlation_id=flow_correlation_id(str(task.attributes["order_id"])),
            key=("warehouse-service", task.id, "complete"),
        )
        engine.context.schedules.at(due_at, command=command)

    return _entity(
        persistence,
        "warehouse_fulfillment_service_task",
        task.id,
    )


def _release_service_capacity(
    persistence: MemoryPersistence,
    engine: Engine,
    backend,
    task: FulfillmentServiceTask,
) -> None:
    if bool(task.attributes.get("resource_released", False)):
        return

    request_id = str(task.attributes["request_id"])
    engine.resources.withdraw(backend, request_id)

    task = _entity(
        persistence,
        "warehouse_fulfillment_service_task",
        task.id,
    )
    task.attributes["resource_released"] = True
    _save_entity(persistence, task)


def _apply_completed_service_task(
    persistence: MemoryPersistence,
    engine: Engine,
    backend,
    *,
    entities: WarehouseEntities,
    task: FulfillmentServiceTask,
) -> None:
    if task.state != "completed":
        return

    if not bool(task.attributes.get("business_applied", False)):
        stage = str(task.attributes["stage"])
        subject_id = str(task.attributes["subject_id"])
        if stage == "pick":
            pick_allocation(
                persistence,
                engine,
                entities=entities,
                allocation_id_value=subject_id,
            )
        elif stage == "pack":
            pack_order(persistence, engine, entities=entities)
        elif stage == "ship":
            ship_order(persistence, engine, entities=entities)
        else:
            raise ValueError(f"unknown fulfillment service stage: {stage}")

        task = _entity(
            persistence,
            "warehouse_fulfillment_service_task",
            task.id,
        )
        task.attributes["business_applied"] = True
        _save_entity(persistence, task)

    task = _entity(
        persistence,
        "warehouse_fulfillment_service_task",
        task.id,
    )
    _release_service_capacity(persistence, engine, backend, task)


def _reconcile_service_task(
    persistence: MemoryPersistence,
    engine: Engine,
    backend,
    *,
    entities: WarehouseEntities,
    stage: str,
    subject_id: str,
    order_id: str,
    resource_name: str,
    service_duration: timedelta,
) -> FulfillmentServiceTask:
    task = _ensure_service_task(
        persistence,
        engine,
        stage=stage,
        subject_id=subject_id,
        order_id=order_id,
        resource_name=resource_name,
        service_duration=service_duration,
    )
    if task.state == "completed":
        _apply_completed_service_task(
            persistence,
            engine,
            backend,
            entities=entities,
            task=task,
        )
        return _entity(
            persistence,
            "warehouse_fulfillment_service_task",
            task.id,
        )

    return _start_service_task_if_capacity_available(
        persistence,
        engine,
        backend,
        task=task,
    )

"""Record-to-report period close and reopen workflow (split from simulation.py)."""

from __future__ import annotations

from datetime import timedelta

from sose.backends.simpy import SimPyBackend
from sose.core.engine import Engine
from sose.persistence.memory import MemoryPersistence

from .identity import flow_correlation_id
from .entities import (
    CloseTask,
)
from .foundation import (
    CLOSE_DELAY,
    R2REntities,
    _close_task,
    _journal,
    _period,
    _reconciliation,
)


def _dispatch(*args, **kwargs):
    """Forward to ``simulation._dispatch`` at call time so patches on it still apply."""
    from . import simulation

    return simulation._dispatch(*args, **kwargs)


def _scheduled_close_work(
    persistence: MemoryPersistence,
    task_id: str,
):
    for work in persistence.scheduled_work():
        command = persistence.command(work.command_id)
        if (
            command is not None
            and command.entity_type == "close_task"
            and command.entity_id == task_id
            and command.name == "start"
        ):
            return work, command
    return None


def schedule_close(
    persistence: MemoryPersistence,
    engine: Engine,
    backend: SimPyBackend,
    *,
    task_id: str,
    period_id: str,
    delay: timedelta = CLOSE_DELAY,
):
    task = _close_task(persistence, task_id)
    if task.state == "in_progress":
        return backend.now
    if task.state != "pending":
        raise RuntimeError(f"close task cannot be scheduled from {task.state}")

    existing = _scheduled_close_work(persistence, task.id)
    if existing is not None:
        return existing[0].due_at

    due_at = backend.now + delay
    command = engine.context.commands.create(
        "start",
        target=task,
        due_at=due_at,
        correlation_id=flow_correlation_id(period_id),
        key=("r2r-close", task.id, "start"),
    )
    engine.context.schedules.at(due_at, command=command)
    return due_at


def _period_ready_for_close(
    persistence: MemoryPersistence,
    entities: R2REntities,
) -> bool:
    journal = _journal(persistence, entities)
    item = _reconciliation(persistence, entities)
    return journal.state == "posted" and item.state in {"matched", "reconciled"}


def reconcile_close(
    persistence: MemoryPersistence,
    engine: Engine,
    backend: SimPyBackend,
    *,
    entities: R2REntities,
    task_id: str | None = None,
) -> bool:
    task_id = task_id or entities.close_task_id
    task = _close_task(persistence, task_id)
    period = _period(persistence, entities)

    request_id = f"close-accountant:{task.id}"
    if period.state == "closed" and task.state == "completed":
        engine.resources.withdraw(backend, request_id)
        return True
    if task.state != "in_progress":
        return False
    if not _period_ready_for_close(persistence, entities):
        return False

    available = bool(
        engine.context.scenarios.attribute("r2r.close_team.available", True)
    )
    if not available:
        engine.resources.withdraw(backend, request_id)
        return False

    reservation = engine.resources.ensure_requested(
        backend,
        resource_name="close_accountant",
        request_id=request_id,
        requested_at=backend.now,
    )
    if reservation is None:
        return False

    correlation_id = flow_correlation_id(period.id)
    _reconcile_period_close_transitions(
        persistence,
        engine,
        entities=entities,
        correlation_id=correlation_id,
    )
    _complete_close_task_if_active(
        persistence,
        engine,
        task_id=task.id,
        correlation_id=correlation_id,
    )

    engine.resources.withdraw(backend, request_id)
    return _period(persistence, entities).state == "closed"


def _reconcile_period_close_transitions(
    persistence: MemoryPersistence,
    engine: Engine,
    *,
    entities: R2REntities,
    correlation_id: str,
) -> None:
    period = _period(persistence, entities)
    if period.state in {"open", "reopened"}:
        _dispatch(
            engine,
            period,
            "prepare_close",
            key=("r2r-period", period.id, "prepare-close", period.version),
            correlation_id=correlation_id,
        )
        period = _period(persistence, entities)

    if period.state == "close_ready":
        _dispatch(
            engine,
            period,
            "close",
            key=("r2r-period", period.id, "close", period.version),
            correlation_id=correlation_id,
        )


def _complete_close_task_if_active(
    persistence: MemoryPersistence,
    engine: Engine,
    *,
    task_id: str,
    correlation_id: str,
) -> None:
    task = _close_task(persistence, task_id)
    if task.state != "in_progress":
        return
    _dispatch(
        engine,
        task,
        "complete",
        key=("r2r-close", task.id, "complete"),
        correlation_id=correlation_id,
    )


def reopen_period(
    persistence: MemoryPersistence,
    engine: Engine,
    *,
    entities: R2REntities,
) -> CloseTask:
    period = _period(persistence, entities)
    if period.state != "closed":
        raise RuntimeError("only a closed period can be reopened")

    correlation_id = flow_correlation_id(period.id)
    _dispatch(
        engine,
        period,
        "reopen",
        key=("r2r-period", period.id, "reopen", period.version),
        correlation_id=correlation_id,
    )
    period = _period(persistence, entities)

    ordinal = int(period.attributes.get("reopen_count", 0)) + 2
    period.attributes["reopen_count"] = ordinal - 1
    task = engine.context.entities.create(
        CloseTask,
        key=("r2r-reference", period.id, "close-task", ordinal),
        state="pending",
        attributes={"period_id": period.id, "ordinal": ordinal},
    )
    with persistence.transaction() as uow:
        uow.save_entity(period)
        uow.save_entity(task)
    return task

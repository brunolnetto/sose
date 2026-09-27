from __future__ import annotations

from datetime import timedelta

from sose.backends.simpy import SimPyBackend
from sose.core.engine import Engine
from sose.persistence.memory import MemoryPersistence

from .runtime import (
    ConstructionEntities,
    activity,
    flow_correlation_id,
)


PLANNED_START_DELAY = timedelta(hours=1)


def _planned_start_work(
    persistence: MemoryPersistence,
    activity_id: str,
):
    for work in persistence.scheduled_work():
        command = persistence.command(work.command_id)
        if (
            command is not None
            and command.entity_type == "construction_activity"
            and command.entity_id == activity_id
            and command.name == "request_resources"
        ):
            return work, command
    return None


def schedule_planned_start(
    persistence: MemoryPersistence,
    engine: Engine,
    backend: SimPyBackend,
    *,
    entities: ConstructionEntities,
    delay: timedelta = PLANNED_START_DELAY,
):
    current = activity(persistence, entities.activity_id)
    if current.state == "waiting_resource":
        return backend.now
    if current.state != "ready":
        raise RuntimeError(
            f"planned start requires Activity(ready), got {current.state}"
        )
    if not bool(current.attributes.get("material_staged", False)):
        raise RuntimeError(
            "planned start requires durable material staging evidence"
        )

    existing = _planned_start_work(persistence, current.id)
    if existing is not None:
        return existing[0].due_at

    due_at = backend.now + delay
    command = engine.context.commands.create(
        "request_resources",
        target=current,
        due_at=due_at,
        correlation_id=flow_correlation_id(current.id),
        key=("construction", current.id, "planned-start"),
    )
    engine.context.schedules.at(due_at, command=command)
    return due_at

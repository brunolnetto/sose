from datetime import datetime

from sose.domain.config import DomainDefinition
from sose.persistence.base import Persistence

from .config import ConstructionConfig
from .execution import (
    complete_activity,
    finish_execution,
    reconcile_dependency,
    reconcile_inspection,
    record_measurement,
    request_execution_resources,
)
from .materials import (
    reconcile_material_availability,
    seed_material,
    stage_material,
)
from .runtime import activity, build_runtime, inspection_id, seed_reference
from .scheduling import schedule_planned_start


def _build(
    persistence: Persistence,
    config: ConstructionConfig,
    now: datetime,
    tick: int,
):
    return build_runtime(
        persistence,
        now=now,
        tick=tick,
        step=config.tick_step,
        random_seed=config.random_seed,
    )


def _seed(persistence: Persistence, config: ConstructionConfig):
    return seed_reference(
        persistence,
        now=config.start_at,
        quantity=config.quantity,
        predecessor_completed=config.predecessor_completed,
    )


def _reconcile_tick(persistence, engine, backend, config, entities) -> None:
    current = activity(persistence, entities.activity_id)

    if current.state in {"planned", "blocked_dependency"}:
        reconcile_dependency(persistence, engine, entities=entities)
        return

    if current.state in {"ready", "waiting_material"} and not bool(
        current.attributes.get("material_staged", False)
    ):
        if config.auto_seed_material:
            seed_material(
                persistence,
                engine,
                backend,
                quantity=config.quantity,
            )
        reconcile_material_availability(
            persistence,
            engine,
            entities=entities,
        )
        current = activity(persistence, entities.activity_id)
        if current.state == "ready":
            stage_material(
                persistence,
                engine,
                backend,
                entities=entities,
            )
        return

    if current.state == "ready":
        schedule_planned_start(
            persistence,
            engine,
            backend,
            entities=entities,
            delay=config.planned_start_delay,
        )
        return

    if current.state == "waiting_resource":
        request_execution_resources(
            persistence,
            engine,
            backend,
            entities=entities,
        )
        return

    if current.state == "rework":
        request_execution_resources(
            persistence,
            engine,
            backend,
            entities=entities,
            cycle="rework-1",
        )
        return

    if current.state == "executing":
        cycle = "rework-1" if persistence.entity(
            "construction_inspection",
            inspection_id(entities.activity_id, 1),
        ) is not None else "initial"
        finish_execution(
            persistence,
            engine,
            backend,
            entities=entities,
            cycle=cycle,
        )
        return

    if current.state == "inspection":
        first = next(
            (
                item
                for item in (
                    persistence.entity(
                        "construction_inspection",
                        inspection_id(entities.activity_id, 1),
                    ),
                )
                if item is not None
            ),
            None,
        )
        ordinal = 2 if first is not None and first.state == "failed" else 1
        reconcile_inspection(
            persistence,
            engine,
            backend,
            entities=entities,
            ordinal=ordinal,
            outcome=config.inspection_outcome,
        )
        return

    if current.state == "measured":
        record_measurement(
            persistence,
            engine,
            entities=entities,
            value=config.quantity,
        )
        complete_activity(
            persistence,
            engine,
            entities=entities,
        )


definition = DomainDefinition(
    name="construction",
    description="Construction planning and execution reference domain.",
    config_model=ConstructionConfig,
    build_runtime=_build,
    seed=_seed,
    reconcile_tick=_reconcile_tick,
    runtime_mutable_fields=frozenset(["tick_step","random_seed","auto_seed_material","inspection_outcome","planned_start_delay"]),
)

from datetime import datetime

from sose.domain.config import DomainDefinition
from sose.persistence.base import Persistence

from .config import ManufacturingConfig
from .simulation import (
    build_runtime,
    reconcile_material_availability,
    reconcile_material_issue,
    reconcile_quality_hold,
    reconcile_quality_pass,
    reconcile_setup_resources,
    reconcile_wip_output,
    release_setup_resources,
    seed_happy_path,
    seed_material,
)


def _build(
    persistence: Persistence,
    config: ManufacturingConfig,
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


def _seed(persistence: Persistence, config: ManufacturingConfig):
    return seed_happy_path(
        persistence,
        now=config.start_at,
        quantity=config.quantity,
    )


def _reconcile_tick(persistence, engine, backend, config, entities) -> None:
    order = persistence.entity(
        "production_order",
        entities.production_order_id,
    )
    if order is None:
        raise RuntimeError("production order was not persisted")

    if config.auto_seed_material and order.state in {
        "planned",
        "released",
        "waiting_material",
    }:
        seed_material(engine, backend, quantity=config.quantity)

    if order.state == "waiting_material":
        reconcile_material_availability(
            persistence,
            engine,
            entities=entities,
        )
        return

    if order.state == "released":
        reconcile_material_availability(
            persistence,
            engine,
            entities=entities,
        )
        order = persistence.entity(
            "production_order",
            entities.production_order_id,
        )
        if order is not None and order.state == "released":
            reconcile_setup_resources(
                persistence,
                engine,
                backend,
                entities=entities,
            )
        return

    if order.state == "setup":
        reconcile_material_issue(
            persistence,
            engine,
            backend,
            entities=entities,
            quantity=config.quantity,
        )
        return

    if order.state == "producing":
        reconcile_wip_output(
            persistence,
            engine,
            backend,
            entities=entities,
            quantity=config.quantity,
        )
        return

    if order.state == "inspection":
        if config.quality_outcome == "pass":
            reconcile_quality_pass(
                persistence,
                engine,
                backend,
                entities=entities,
                quantity=config.quantity,
            )
        else:
            reconcile_quality_hold(
                persistence,
                engine,
                entities=entities,
            )
        return

    if order.state == "completed":
        release_setup_resources(
            persistence,
            engine,
            backend,
            entities=entities,
        )
        return



definition = DomainDefinition(
    name="manufacturing",
    description="Manufacturing production-flow reference domain.",
    config_model=ManufacturingConfig,
    build_runtime=_build,
    seed=_seed,
    reconcile_tick=_reconcile_tick,
)

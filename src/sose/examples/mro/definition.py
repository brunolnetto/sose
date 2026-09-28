from __future__ import annotations

from datetime import datetime

from sose.domain.config import DomainDefinition
from sose.persistence.base import Persistence

from .config import MROConfig
from .simulation import (
    MROEntities,
    build_runtime,
    reconcile_scenario_emergency,
    reconcile_start,
    seed_reference,
    seed_spare_parts,
)


def _build(
    persistence: Persistence,
    config: MROConfig,
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


def _seed(persistence: Persistence, config: MROConfig):
    return seed_reference(
        persistence,
        quantity=config.quantity,
        now=config.start_at,
        technician_capacity=config.technician_capacity,
        maintenance_bay_capacity=config.maintenance_bay_capacity,
        spare_part_store_capacity=config.spare_part_store_capacity,
        release_delay=config.release_delay,
    )


def _reconcile_tick(
    persistence: Persistence,
    engine,
    backend,
    config: MROConfig,
    entities: MROEntities,
) -> None:
    if config.auto_seed_spare_parts:
        seed_spare_parts(engine, backend, quantity=config.quantity)
    reconcile_start(
        persistence,
        engine,
        backend,
        entities=entities,
        quantity=config.quantity,
    )
    reconcile_scenario_emergency(
        persistence,
        engine,
        backend,
        entities=entities,
    )


definition = DomainDefinition(
    name="mro",
    description="Maintenance, repair and operations reference domain.",
    config_model=MROConfig,
    build_runtime=_build,
    seed=_seed,
    reconcile_tick=_reconcile_tick,
)

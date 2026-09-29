from datetime import datetime

from sose.domain.config import DomainDefinition
from sose.persistence.base import Persistence

from .config import TransitConfig
from .simulation import (
    build_runtime,
    reconcile_vehicle_for_trip,
    schedule_reference_block,
    seed_reference,
)


def _build(
    persistence: Persistence,
    config: TransitConfig,
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


def _seed(persistence: Persistence, config: TransitConfig):
    entities = seed_reference(
        persistence,
        now=config.start_at,
        vehicle_key=config.vehicle_key,
        block_id=config.block_id,
        first_trip_delay=config.first_trip_delay,
        trip_duration=config.trip_duration,
        layover=config.layover,
    )
    _, engine = build_runtime(
        persistence,
        now=config.start_at,
        step=config.tick_step,
        random_seed=config.random_seed,
    )
    schedule_reference_block(
        persistence,
        engine,
        entities=entities,
    )
    return entities


def _reconcile_tick(persistence, engine, backend, config, entities) -> None:
    if not config.auto_reconcile_vehicle:
        return
    # Ensure projected start/end boundaries exist. advance_tick owns their
    # execution; this reconciler only aligns vehicle business state afterward.
    schedule_reference_block(
        persistence,
        engine,
        entities=entities,
    )
    for trip_id in (entities.trip_a_id, entities.trip_b_id):
        reconcile_vehicle_for_trip(
            persistence,
            engine,
            entities=entities,
            trip_id=trip_id,
        )


definition = DomainDefinition(
    name="transit",
    description="Public transit and rail operations reference domain.",
    config_model=TransitConfig,
    build_runtime=_build,
    seed=_seed,
    reconcile_tick=_reconcile_tick,
    runtime_mutable_fields=frozenset(["tick_step","random_seed","auto_reconcile_vehicle"]),
)

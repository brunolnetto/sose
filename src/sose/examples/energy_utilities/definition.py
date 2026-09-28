from datetime import datetime

from sose.domain.config import DomainDefinition
from sose.persistence.base import Persistence

from .config import EnergyUtilitiesConfig
from .simulation import (
    build_runtime,
    record_meter_reading,
    reconcile_demand_response,
    schedule_demand_response,
    seed_reference,
)


def _build(
    persistence: Persistence,
    config: EnergyUtilitiesConfig,
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


def _seed(persistence: Persistence, config: EnergyUtilitiesConfig):
    return seed_reference(
        persistence,
        now=config.start_at,
        primary_customer_id=config.primary_customer_id,
        secondary_customer_id=config.secondary_customer_id,
        include_secondary=config.include_secondary,
        quantity_kind=config.quantity_kind,
        unit=config.unit,
    )


def _reconcile_tick(persistence, engine, backend, config, entities) -> None:
    if config.auto_meter_reading:
        record_meter_reading(
            persistence,
            engine,
            entities=entities,
            interval_end=backend.now,
            quantity_kwh=config.meter_reading_quantity,
        )

    if not config.demand_response_enabled:
        return

    schedule_demand_response(
        persistence,
        engine,
        backend,
        entities=entities,
        event_key=config.demand_response_event_key,
        start_delay=config.demand_response_start_delay,
        duration=config.demand_response_duration,
    )
    reconcile_demand_response(
        persistence,
        engine,
        entities=entities,
        event_key=config.demand_response_event_key,
    )


definition = DomainDefinition(
    name="energy_utilities",
    description="Energy and utilities reference domain.",
    config_model=EnergyUtilitiesConfig,
    build_runtime=_build,
    seed=_seed,
    reconcile_tick=_reconcile_tick,
)

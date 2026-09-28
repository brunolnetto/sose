from datetime import datetime

from sose.domain.config import DomainDefinition
from sose.persistence.base import Persistence

from .config import AviationConfig
from .simulation import (
    build_runtime,
    complete_aog_maintenance,
    land_flight,
    maintenance_work_order_id,
    reconcile_aog_maintenance,
    reconcile_departure,
    reconcile_inspection,
    reconcile_part_issue,
    schedule_departure,
    seed_reference,
    seed_spare_part,
)


def _build(
    persistence: Persistence,
    config: AviationConfig,
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


def _seed(persistence: Persistence, config: AviationConfig):
    return seed_reference(
        persistence,
        now=config.start_at,
        tail_number=config.tail_number,
        leg1_number=config.leg1_number,
        leg2_number=config.leg2_number,
        flight_crew_capacity=config.flight_crew_capacity,
        inspection_team_capacity=config.inspection_team_capacity,
        maintenance_bay_capacity=config.maintenance_bay_capacity,
        maintenance_queue_capacity=config.maintenance_queue_capacity,
        part_lot_capacity=config.part_lot_capacity,
    )


def _reconcile_aog_branch(
    persistence,
    engine,
    backend,
    config,
    entities,
    *,
    flight_id: str,
) -> bool:
    work = persistence.entity(
        "aviation_maintenance_work_order",
        maintenance_work_order_id(flight_id),
    )
    if work is None:
        return False

    if config.auto_seed_aog_part:
        seed_spare_part(persistence, engine, backend)

    demand = persistence.entity(
        "aviation_part_demand",
        work.attributes.get("part_demand_id", ""),
    )
    if demand is None or demand.state != "issued":
        reconcile_part_issue(
            persistence,
            engine,
            backend,
            entities=entities,
            flight_id=flight_id,
        )
        return True

    work = persistence.entity("aviation_maintenance_work_order", work.id)
    if work is not None and work.state in {"released", "waiting_bay"}:
        reconcile_aog_maintenance(
            persistence,
            engine,
            backend,
            entities=entities,
            flight_id=flight_id,
        )
        return True

    if work is not None and work.state == "in_progress":
        complete_aog_maintenance(
            persistence,
            engine,
            backend,
            entities=entities,
            flight_id=flight_id,
        )
        return True

    return False


def _reconcile_tick(persistence, engine, backend, config, entities) -> None:
    schedule_departure(
        persistence,
        engine,
        backend,
        flight_id=entities.leg1_id,
        delay=config.leg1_departure_delay,
    )
    schedule_departure(
        persistence,
        engine,
        backend,
        flight_id=entities.leg2_id,
        delay=config.leg2_departure_delay,
    )

    for flight_id in (entities.leg1_id, entities.leg2_id):
        flight = persistence.entity("aviation_flight", flight_id)
        if flight is None:
            raise RuntimeError(f"aviation flight was not persisted: {flight_id}")

        if _reconcile_aog_branch(
            persistence,
            engine,
            backend,
            config,
            entities,
            flight_id=flight_id,
        ):
            return

        if flight.state in {"due", "delayed", "ready"}:
            reconcile_departure(
                persistence,
                engine,
                backend,
                entities=entities,
                flight_id=flight.id,
            )
            return

        if flight.state == "airborne" and config.auto_land:
            land_flight(
                persistence,
                engine,
                backend,
                entities=entities,
                flight_id=flight.id,
            )
            return

        if flight.state == "inspection":
            reconcile_inspection(
                persistence,
                engine,
                backend,
                entities=entities,
                flight_id=flight.id,
                fail=config.inspection_fail,
            )
            return


definition = DomainDefinition(
    name="aviation",
    description="Aviation operations and preemption reference domain.",
    config_model=AviationConfig,
    build_runtime=_build,
    seed=_seed,
    reconcile_tick=_reconcile_tick,
)

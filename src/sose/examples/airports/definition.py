from datetime import datetime
from sose.domain.config import DomainDefinition
from sose.persistence.base import Persistence
from .config import AirportsConfig
from .simulation import (
    build_runtime,
    queue_departure,
    reconcile_baggage,
    reconcile_departure,
    reconcile_gate,
    reconcile_ground_service,
    schedule_arrival,
    schedule_departure_slot,
    seed_reference,
)

def _build(persistence: Persistence, config: AirportsConfig, now: datetime, tick: int):
    return build_runtime(persistence, now=now, tick=tick, step=config.tick_step, random_seed=config.random_seed)

def _seed(persistence: Persistence, config: AirportsConfig):
    return seed_reference(
        persistence,
        now=config.start_at,
        flight_number=config.flight_number,
        departure_priority=config.departure_priority,
        gate=config.gate,
        gate_capacity=config.gate_capacity,
        ground_team_capacity=config.ground_team_capacity,
        tug_capacity=config.tug_capacity,
        departure_queue_capacity=config.departure_queue_capacity,
    )

def _reconcile_tick(persistence, engine, backend, config, entities):
    turnaround = persistence.entity(
        "airport_flight_turnaround",
        entities.turnaround_id,
    )
    if turnaround is None:
        raise RuntimeError("configured turnaround was not persisted")
    if turnaround.state == "departed":
        return

    if turnaround.state == "scheduled":
        schedule_arrival(
            persistence,
            engine,
            backend,
            entities=entities,
            delay=config.arrival_delay,
        )
        schedule_departure_slot(
            persistence,
            engine,
            backend,
            entities=entities,
            delay=config.departure_slot_delay,
        )
        return

    if turnaround.state in {"arrived", "gate_hold"}:
        if not reconcile_gate(
            persistence,
            engine,
            backend,
            entities=entities,
        ):
            return
        turnaround = persistence.entity(
            "airport_flight_turnaround",
            entities.turnaround_id,
        )

    if turnaround is not None and turnaround.state in {
        "gate_assigned",
        "deboarding",
        "servicing",
    }:
        if not reconcile_ground_service(
            persistence,
            engine,
            backend,
            entities=entities,
        ):
            return
        turnaround = persistence.entity(
            "airport_flight_turnaround",
            entities.turnaround_id,
        )

    if turnaround is not None and turnaround.state in {
        "boarding",
        "waiting_baggage",
    }:
        if not reconcile_baggage(
            persistence,
            engine,
            entities=entities,
        ):
            return
        turnaround = persistence.entity(
            "airport_flight_turnaround",
            entities.turnaround_id,
        )

    if turnaround is not None and turnaround.state == "boarding":
        queue_departure(
            persistence,
            engine,
            backend,
            entities=entities,
        )
        turnaround = persistence.entity(
            "airport_flight_turnaround",
            entities.turnaround_id,
        )

    if turnaround is not None and turnaround.state in {
        "waiting_slot",
        "pushback",
    }:
        reconcile_departure(
            persistence,
            engine,
            backend,
            entities=entities,
        )

definition = DomainDefinition(
    name="airports",
    description="Airport turnaround and departure operations reference domain.",
    config_model=AirportsConfig,
    build_runtime=_build,
    seed=_seed,
    reconcile_tick=_reconcile_tick,
    runtime_mutable_fields=frozenset(["tick_step","random_seed","arrival_delay","departure_slot_delay"]),
)

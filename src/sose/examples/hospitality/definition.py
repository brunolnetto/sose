from datetime import datetime
from sose.domain.config import DomainDefinition
from sose.persistence.base import Persistence
from .config import HospitalityConfig
from .simulation import (
    check_in,
    check_out,
    confirm_reservation,
    create_hold,
    reservation_id,
    build_runtime,
    seed_reference,
)

def _build(persistence: Persistence, config: HospitalityConfig, now: datetime, tick: int):
    return build_runtime(persistence, now=now, tick=tick, step=config.tick_step, random_seed=config.random_seed)

def _seed(persistence: Persistence, config: HospitalityConfig):
    return seed_reference(
        persistence,
        now=config.start_at,
        room_count=config.room_count,
        room_type=config.room_type,
    )

def _reconcile_tick(persistence, engine, backend, config, entities):
    if not config.auto_progress_reservation:
        return
    rid = reservation_id(1)
    reservation = persistence.entity("hospitality_reservation", rid)

    if reservation is None:
        arrival_at = config.start_at + config.arrival_after
        if arrival_at <= backend.now:
            arrival_at = backend.now + config.tick_step
        reservation = create_hold(
            persistence,
            engine,
            backend,
            entities=entities,
            ordinal=1,
            arrival_at=arrival_at,
            departure_at=arrival_at + config.stay_duration,
            hold_for=config.hold_duration,
            room_type=config.room_type,
        )
        confirm_reservation(
            persistence,
            engine,
            reservation_id_value=reservation.id,
            no_show_grace=config.no_show_grace,
        )
        return

    if reservation.state == "confirmed":
        arrival_at = datetime.fromisoformat(str(reservation.attributes["arrival_at"]))
        if backend.now >= arrival_at:
            check_in(
                persistence,
                engine,
                reservation_id_value=reservation.id,
                at=backend.now,
            )
            reservation = persistence.entity(
                "hospitality_reservation",
                reservation.id,
            )

    if reservation is not None and reservation.state == "checked_in":
        departure_at = datetime.fromisoformat(
            str(reservation.attributes["departure_at"])
        )
        if backend.now >= departure_at:
            check_out(
                persistence,
                engine,
                reservation_id_value=reservation.id,
                at=backend.now,
            )


definition = DomainDefinition(
    name="hospitality",
    description="Hospitality and reservations reference domain.",
    config_model=HospitalityConfig,
    build_runtime=_build,
    seed=_seed,
    reconcile_tick=_reconcile_tick,
    runtime_mutable_fields=frozenset(["tick_step","random_seed","auto_progress_reservation","arrival_after","stay_duration","hold_duration","no_show_grace"]),
)

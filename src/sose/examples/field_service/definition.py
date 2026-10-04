from datetime import datetime
from sose.domain.config import DomainDefinition
from sose.persistence.base import Persistence
from .config import FieldServiceConfig
from .simulation import (
    build_runtime,
    confirm_appointment,
    propose_appointment,
    reconcile_work_start,
    record_visit,
    seed_part_inventory,
    seed_reference,
)

def _build(persistence: Persistence, config: FieldServiceConfig, now: datetime, tick: int):
    return build_runtime(persistence, now=now, tick=tick, step=config.tick_step, random_seed=config.random_seed)

def _seed(persistence: Persistence, config: FieldServiceConfig):
    return seed_reference(
        persistence,
        now=config.start_at,
        territory=config.territory,
        required_skill=config.required_skill,
        wrong_skill=config.wrong_skill,
        technician_resource_capacity=config.technician_resource_capacity,
        parts_store_capacity=config.parts_store_capacity,
    )

def _work_order_or_error(persistence, entities):
    work_order = persistence.entity("field_work_order", entities.work_order_id)
    if work_order is None:
        raise RuntimeError("configured field work order was not persisted")
    return work_order


def _ensure_active_appointment(persistence, engine, backend, config, *, entities, work_order):
    active_id = work_order.attributes.get("active_appointment_id")
    if work_order.state not in {"ready", "reschedule_required"} or active_id is not None:
        return
    ordinal = len(work_order.attributes.get("appointment_ids", [])) + 1
    start_at = backend.now + config.appointment_start_delay
    appointment = propose_appointment(
        persistence,
        engine,
        entities=entities,
        ordinal=ordinal,
        start_at=start_at,
        end_at=start_at + config.appointment_duration,
    )
    confirm_appointment(
        persistence,
        engine,
        entities=entities,
        appointment_id_value=appointment.id,
    )


def _active_appointment(persistence, *, entities):
    work_order = persistence.entity("field_work_order", entities.work_order_id)
    active_id = None if work_order is None else work_order.attributes.get("active_appointment_id")
    if active_id is None:
        return work_order, None
    appointment = persistence.entity("field_appointment", str(active_id))
    return work_order, appointment


def _reconcile_work_start_if_ready(persistence, engine, backend, *, entities, work_order, appointment):
    if appointment is None or work_order is None:
        return work_order, appointment, False
    if appointment.state != "in_progress" or work_order.state != "scheduled":
        return work_order, appointment, True
    started = reconcile_work_start(
        persistence,
        engine,
        backend,
        entities=entities,
        appointment_id_value=appointment.id,
    )
    if not started:
        return work_order, appointment, False
    return _active_appointment(persistence, entities=entities) + (True,)


def _reconcile_tick(persistence, engine, backend, config, entities):
    work_order = _work_order_or_error(persistence, entities)
    if work_order.state == "completed":
        return

    if config.auto_seed_required_part:
        seed_part_inventory(persistence, engine, backend)

    _ensure_active_appointment(
        persistence,
        engine,
        backend,
        config,
        entities=entities,
        work_order=work_order,
    )
    work_order, appointment = _active_appointment(persistence, entities=entities)
    if appointment is None:
        return

    work_order, appointment, should_continue = _reconcile_work_start_if_ready(
        persistence,
        engine,
        backend,
        entities=entities,
        work_order=work_order,
        appointment=appointment,
    )
    if not should_continue:
        return

    if (
        appointment is not None
        and work_order is not None
        and appointment.state == "in_progress"
        and work_order.state == "in_progress"
    ):
        record_visit(
            persistence,
            engine,
            backend,
            entities=entities,
            appointment_id_value=appointment.id,
            sequence=1,
            outcome="completed",
        )


definition = DomainDefinition(
    name="field_service",
    description="Field service and workforce reference domain.",
    config_model=FieldServiceConfig,
    build_runtime=_build,
    seed=_seed,
    reconcile_tick=_reconcile_tick,
    runtime_mutable_fields=frozenset(["tick_step","random_seed","auto_seed_required_part"]),
)

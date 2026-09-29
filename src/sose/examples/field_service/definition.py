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

def _reconcile_tick(persistence, engine, backend, config, entities):
    work_order = persistence.entity("field_work_order", entities.work_order_id)
    if work_order is None:
        raise RuntimeError("configured field work order was not persisted")
    if work_order.state == "completed":
        return

    if config.auto_seed_required_part:
        seed_part_inventory(persistence, engine, backend)

    active_id = work_order.attributes.get("active_appointment_id")
    if work_order.state in {"ready", "reschedule_required"} and active_id is None:
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
        return

    work_order = persistence.entity("field_work_order", entities.work_order_id)
    active_id = None if work_order is None else work_order.attributes.get(
        "active_appointment_id"
    )
    if active_id is None:
        return
    appointment = persistence.entity("field_appointment", str(active_id))
    if appointment is None:
        return

    if appointment.state == "in_progress" and work_order.state == "scheduled":
        if not reconcile_work_start(
            persistence,
            engine,
            backend,
            entities=entities,
            appointment_id_value=appointment.id,
        ):
            return
        work_order = persistence.entity("field_work_order", entities.work_order_id)
        appointment = persistence.entity("field_appointment", appointment.id)

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

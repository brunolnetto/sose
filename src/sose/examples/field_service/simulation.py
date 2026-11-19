from __future__ import annotations

from datetime import timedelta  # noqa: F401

from sose.backends.simpy import SimPyBackend
from sose.core.clock import SimulationClock  # noqa: F401
from sose.core.context import SimulationContext  # noqa: F401
from sose.core.engine import Engine
from sose.core.randomness import RandomSource  # noqa: F401
from sose.core.runtime import ResourceDefinition, StoreDefinition  # noqa: F401
from sose.core.scheduler import Scheduler  # noqa: F401
from sose.domain.registry import DomainRegistry, EntityType  # noqa: F401
from sose.persistence.memory import MemoryPersistence

from .entities import Appointment, Technician, VisitOccurrence, WorkOrder  # noqa: F401
from .scenarios import ORIGIN
from .statecharts import (  # noqa: F401
    AppointmentChart,
    TechnicianChart,
    VisitOccurrenceChart,
    WorkOrderChart,
)
from .foundation import (  # noqa: F401
    FieldServiceEntities,
    PART_FILTER_KEY,
    REQUIRED_PART,
    _appointment,
    _at,
    _dispatch,
    _entity,
    _technician,
    _work_order,
    build_runtime,
    flow_correlation_id,
    seed_part_inventory,
    seed_reference,
    visit_id,
)
from .scheduling import (  # noqa: F401
    confirm_appointment,
    propose_appointment,
)


def reserve_required_part(
    persistence: MemoryPersistence,
    engine: Engine,
    backend: SimPyBackend,
    *,
    entities: FieldServiceEntities,
) -> bool:
    work_order = _work_order(persistence, entities)
    request_id = f"field-part:{work_order.id}"
    existing = next(
        (
            result
            for result in persistence.store_get_results()
            if result.request_id == request_id
        ),
        None,
    )
    if existing is not None:
        if existing.item.value.get("sku") != work_order.attributes["required_part"]:
            raise RuntimeError("reserved field part does not satisfy work order")
        return True

    result = engine.stores.ensure_selection(
        backend,
        store_name="field_parts",
        request_id=request_id,
        requested_at=backend.now,
        filter_key=PART_FILTER_KEY,
    )
    if result is None:
        return False
    if result.item.value.get("sku") != work_order.attributes["required_part"]:
        raise RuntimeError("selected field part does not satisfy work order")
    return True


def reconcile_work_start(
    persistence: MemoryPersistence,
    engine: Engine,
    backend: SimPyBackend,
    *,
    entities: FieldServiceEntities,
    appointment_id_value: str,
) -> bool:
    appointment = _appointment(persistence, appointment_id_value)
    work_order = _work_order(persistence, entities)
    if not _can_reconcile_work_start(
        engine,
        appointment=appointment,
        work_order=work_order,
    ):
        return False
    if not reserve_required_part(
        persistence,
        engine,
        backend,
        entities=entities,
    ):
        return False

    technician_id = appointment.attributes.get("technician_id")
    if technician_id is None:
        raise RuntimeError("appointment has no technician")
    technician = _technician(persistence, str(technician_id))
    request_id = f"field-tech:{appointment.id}"
    if engine.resources.ensure_requested(
        backend,
        resource_name=f"field-tech:{technician.id}",
        request_id=request_id,
        requested_at=backend.now,
    ) is None:
        return False

    correlation_id = flow_correlation_id(work_order.id)
    _dispatch_work_start_events(
        persistence,
        engine,
        entities=entities,
        appointment=appointment,
        technician_id=technician.id,
        correlation_id=correlation_id,
    )

    # The durable booking owns the whole appointment window. Resource capacity is
    # only the start-time concurrency gate, so release it immediately after the
    # semantic assignment has been committed. This prevents an expiry callback
    # from leaking backend capacity while the Technician entity remains the
    # durable assignment truth.
    engine.resources.withdraw(backend, request_id)
    _schedule_technician_window_release(
        persistence,
        engine,
        appointment=appointment,
        technician_id=technician.id,
        correlation_id=correlation_id,
    )
    return True


def _can_reconcile_work_start(
    engine: Engine,
    *,
    appointment: Appointment,
    work_order: WorkOrder,
) -> bool:
    if appointment.state != "in_progress":
        return False
    if work_order.attributes.get("active_appointment_id") != appointment.id:
        return False
    if work_order.state != "scheduled":
        return False
    return bool(engine.context.scenarios.attribute("field_service.dispatch.available", True))


def _dispatch_work_start_events(
    persistence: MemoryPersistence,
    engine: Engine,
    *,
    entities: FieldServiceEntities,
    appointment: Appointment,
    technician_id: str,
    correlation_id: str,
) -> None:
    technician = _technician(persistence, technician_id)
    if technician.state == "available":
        _dispatch(
            engine,
            technician,
            "assign",
            key=("field-technician", technician.id, appointment.id, "assign"),
            correlation_id=correlation_id,
        )
    work_order = _work_order(persistence, entities)
    if work_order.state == "scheduled":
        _dispatch(
            engine,
            work_order,
            "start",
            key=("field-work-order", work_order.id, appointment.id, "start"),
            correlation_id=correlation_id,
        )


def _schedule_technician_window_release(
    persistence: MemoryPersistence,
    engine: Engine,
    *,
    appointment: Appointment,
    technician_id: str,
    correlation_id: str,
) -> None:
    technician = _technician(persistence, technician_id)
    if technician.state != "assigned":
        return
    if engine.scheduler.find_pending(
        entity_type="field_technician",
        entity_id=technician.id,
        name="release",
    ) is not None:
        return
    end_at = _at(appointment.attributes["end_at"])
    command = engine.context.commands.create(
        "release",
        target=technician,
        due_at=end_at,
        correlation_id=correlation_id,
        key=("field-technician", technician.id, appointment.id, "window-end"),
    )
    engine.context.schedules.at(end_at, command=command)


def _release_technician(
    persistence: MemoryPersistence,
    engine: Engine,
    backend: SimPyBackend,
    *,
    appointment: Appointment,
    work_order_id: str,
) -> None:
    technician_id = appointment.attributes.get("technician_id")
    if technician_id is None:
        return
    engine.resources.withdraw(backend, f"field-tech:{appointment.id}")
    technician = _technician(persistence, str(technician_id))
    engine.scheduler.cancel_pending(
        entity_type="field_technician",
        entity_id=technician.id,
        name="release",
    )
    if technician.state == "assigned":
        _dispatch(
            engine,
            technician,
            "release",
            key=("field-technician", technician.id, appointment.id, "release"),
            correlation_id=flow_correlation_id(work_order_id),
        )


def record_visit(
    persistence: MemoryPersistence,
    engine: Engine,
    backend: SimPyBackend,
    *,
    entities: FieldServiceEntities,
    appointment_id_value: str,
    sequence: int,
    outcome: str,
) -> VisitOccurrence:
    if sequence <= 0:
        raise ValueError("visit sequence must be positive")
    if outcome not in {"completed", "no_access"}:
        raise ValueError(f"unsupported visit outcome: {outcome}")

    appointment = _appointment(persistence, appointment_id_value)
    work_order = _work_order(persistence, entities)
    vid = visit_id(appointment.id, sequence)
    existing = persistence.entity("field_visit_occurrence", vid)
    if existing is not None:
        if existing.attributes["outcome"] != outcome:
            raise ValueError("visit identity already exists with different outcome")
        return existing
    if appointment.state != "in_progress" or work_order.state != "in_progress":
        raise RuntimeError("visit outcome requires active appointment and work order")

    visit = engine.context.entities.create(
        VisitOccurrence,
        key=("field-service-reference", appointment.id, "visit", sequence),
        state="captured",
        attributes={
            "work_order_id": work_order.id,
            "appointment_id": appointment.id,
            "technician_id": appointment.attributes["technician_id"],
            "outcome": outcome,
        },
    )
    with persistence.transaction() as uow:
        uow.save_entity(visit)
    correlation_id = flow_correlation_id(work_order.id)
    _dispatch(
        engine,
        visit,
        "commit",
        key=("field-visit", visit.id, "commit"),
        correlation_id=correlation_id,
    )

    engine.scheduler.cancel_pending(
        entity_type="field_appointment",
        entity_id=appointment.id,
        name="miss",
    )
    engine.scheduler.cancel_pending(
        entity_type="field_work_order",
        entity_id=work_order.id,
        name="require_reschedule",
    )
    appointment = _appointment(persistence, appointment.id)
    work_order = _work_order(persistence, entities)
    if outcome == "no_access":
        _dispatch(
            engine,
            appointment,
            "no_access",
            key=("field-appointment", appointment.id, "no-access"),
            correlation_id=correlation_id,
        )
        _dispatch(
            engine,
            work_order,
            "require_reschedule",
            key=("field-work-order", work_order.id, appointment.id, "no-access"),
            correlation_id=correlation_id,
        )
    else:
        _dispatch(
            engine,
            appointment,
            "complete",
            key=("field-appointment", appointment.id, "complete"),
            correlation_id=correlation_id,
        )
        _dispatch(
            engine,
            work_order,
            "complete",
            key=("field-work-order", work_order.id, appointment.id, "complete"),
            correlation_id=correlation_id,
        )

    _release_technician(
        persistence,
        engine,
        backend,
        appointment=appointment,
        work_order_id=work_order.id,
    )
    work_order = _work_order(persistence, entities)
    if work_order.attributes.get("active_appointment_id") == appointment.id:
        work_order.attributes["active_appointment_id"] = None
        with persistence.transaction() as uow:
            uow.save_entity(work_order)
    return _entity(persistence, "field_visit_occurrence", visit.id)


def run_happy_path(*, owner_id: str) -> tuple[MemoryPersistence, FieldServiceEntities]:
    persistence = MemoryPersistence()
    entities = seed_reference(persistence, owner_id=owner_id)
    _, engine = build_runtime(persistence, owner_id=owner_id)
    backend = SimPyBackend(origin=ORIGIN)
    engine.rebuild_backend(backend)
    seed_part_inventory(persistence, engine, backend)

    appointment = propose_appointment(
        persistence,
        engine,
        entities=entities,
        ordinal=1,
        start_at=ORIGIN + timedelta(hours=1),
        end_at=ORIGIN + timedelta(hours=2),
    )
    appointment = confirm_appointment(
        persistence,
        engine,
        entities=entities,
        appointment_id_value=appointment.id,
    )
    backend.run_until(_at(appointment.attributes["start_at"]))
    if not reconcile_work_start(
        persistence,
        engine,
        backend,
        entities=entities,
        appointment_id_value=appointment.id,
    ):
        raise RuntimeError("field work did not start")
    record_visit(
        persistence,
        engine,
        backend,
        entities=entities,
        appointment_id_value=appointment.id,
        sequence=1,
        outcome="completed",
    )
    return persistence, entities

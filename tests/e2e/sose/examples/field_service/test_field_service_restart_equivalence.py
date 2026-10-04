from datetime import timedelta

from sose.backends.simpy import SimPyBackend
from sose.examples.field_service.scenarios import ORIGIN
from sose.examples.field_service.simulation import (
    build_runtime,
    confirm_appointment,
    propose_appointment,
    record_visit,
    reconcile_work_start,
    seed_part_inventory,
    seed_reference,
)
from sose.persistence.memory import MemoryPersistence
from sose.testing.restart import restart_reference_runtime


def test_replacement_appointment_part_and_booking_survive_restart():
    persistence = MemoryPersistence()
    entities = seed_reference(persistence)
    _, engine = build_runtime(persistence)
    backend = SimPyBackend(origin=ORIGIN)
    engine.rebuild_backend(backend)
    seed_part_inventory(persistence, engine, backend)

    first = propose_appointment(
        persistence,
        engine,
        entities=entities,
        ordinal=1,
        start_at=ORIGIN + timedelta(hours=1),
        end_at=ORIGIN + timedelta(hours=2),
    )
    first = confirm_appointment(
        persistence,
        engine,
        entities=entities,
        appointment_id_value=first.id,
    )
    backend.run_until(ORIGIN + timedelta(hours=1))
    assert reconcile_work_start(
        persistence,
        engine,
        backend,
        entities=entities,
        appointment_id_value=first.id,
    )
    record_visit(
        persistence,
        engine,
        backend,
        entities=entities,
        appointment_id_value=first.id,
        sequence=1,
        outcome="no_access",
    )

    replacement = propose_appointment(
        persistence,
        engine,
        entities=entities,
        ordinal=2,
        start_at=ORIGIN + timedelta(hours=3),
        end_at=ORIGIN + timedelta(hours=4),
        replaces_appointment_id=first.id,
    )
    replacement = confirm_appointment(
        persistence,
        engine,
        entities=entities,
        appointment_id_value=replacement.id,
    )
    assert len(persistence.store_get_results()) == 1

    rebuilt = restart_reference_runtime(
        persistence,
        build_runtime,
        backend,
        backend_factory=SimPyBackend,
    )
    rebuilt.backend.run_until(ORIGIN + timedelta(hours=3))
    assert reconcile_work_start(
        persistence,
        rebuilt.engine,
        rebuilt.backend,
        entities=entities,
        appointment_id_value=replacement.id,
    )
    record_visit(
        persistence,
        rebuilt.engine,
        rebuilt.backend,
        entities=entities,
        appointment_id_value=replacement.id,
        sequence=1,
        outcome="completed",
    )

    work_order = persistence.entity("field_work_order", entities.work_order_id)
    replacement = persistence.entity("field_appointment", replacement.id)
    assert work_order is not None and work_order.state == "completed"
    assert replacement is not None and replacement.state == "completed"
    assert len(persistence.store_get_results()) == 1
    assert persistence.resource_reservations() == ()
    assert persistence.scheduled_work() == ()

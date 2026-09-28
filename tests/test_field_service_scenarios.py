from datetime import timedelta

from sose.backends.simpy import SimPyBackend
from sose.examples.field_service.scenarios import ORIGIN, technician_dispatch_outage
from sose.examples.field_service.simulation import (
    build_runtime,
    confirm_appointment,
    propose_appointment,
    reconcile_work_start,
    seed_part_inventory,
    seed_reference,
)
from sose.persistence.memory import MemoryPersistence


def test_dispatch_outage_defers_execution_without_inventing_ownership():
    persistence = MemoryPersistence()
    entities = seed_reference(persistence)
    context, engine = build_runtime(
        persistence,
        scenarios=(technician_dispatch_outage(),),
    )
    backend = SimPyBackend(origin=ORIGIN)
    engine.rebuild_backend(backend)
    seed_part_inventory(persistence, engine, backend)

    appointment = propose_appointment(
        persistence,
        engine,
        entities=entities,
        ordinal=1,
        start_at=ORIGIN + timedelta(hours=1),
        end_at=ORIGIN + timedelta(hours=4),
    )
    appointment = confirm_appointment(
        persistence,
        engine,
        entities=entities,
        appointment_id_value=appointment.id,
    )

    engine.advance_tick()
    backend.run_until(ORIGIN + timedelta(hours=1))
    assert reconcile_work_start(
        persistence,
        engine,
        backend,
        entities=entities,
        appointment_id_value=appointment.id,
    ) is False
    assert persistence.resource_reservations() == ()

    for _ in range(2):
        engine.advance_tick()
    assert context.clock.now >= ORIGIN + timedelta(hours=3)
    assert reconcile_work_start(
        persistence,
        engine,
        backend,
        entities=entities,
        appointment_id_value=appointment.id,
    )

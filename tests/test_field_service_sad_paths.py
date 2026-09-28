from datetime import timedelta

import pytest

from sose.backends.simpy import SimPyBackend
from sose.examples.field_service.scenarios import ORIGIN
from sose.examples.field_service.simulation import (
    REQUIRED_PART,
    appointment_id,
    build_runtime,
    confirm_appointment,
    propose_appointment,
    record_visit,
    reconcile_work_start,
    reserve_required_part,
    seed_part_inventory,
    seed_reference,
    select_technician,
    visit_id,
)
from sose.persistence.memory import MemoryPersistence


def _runtime(*, seed_part=True):
    persistence = MemoryPersistence()
    entities = seed_reference(persistence)
    _, engine = build_runtime(persistence)
    backend = SimPyBackend(origin=ORIGIN)
    engine.rebuild_backend(backend)
    if seed_part:
        seed_part_inventory(persistence, engine, backend)
    return persistence, entities, engine, backend


def test_appointment_requires_valid_window():
    persistence, entities, engine, _ = _runtime()
    with pytest.raises(ValueError, match="end must be after start"):
        propose_appointment(
            persistence,
            engine,
            entities=entities,
            ordinal=1,
            start_at=ORIGIN + timedelta(hours=2),
            end_at=ORIGIN + timedelta(hours=1),
        )


def test_skill_territory_and_window_eligibility_prevents_double_booking():
    persistence, entities, engine, _ = _runtime()
    first = propose_appointment(
        persistence,
        engine,
        entities=entities,
        ordinal=1,
        start_at=ORIGIN + timedelta(hours=1),
        end_at=ORIGIN + timedelta(hours=2),
    )
    confirmed = confirm_appointment(
        persistence,
        engine,
        entities=entities,
        appointment_id_value=first.id,
    )
    selected = confirmed.attributes["technician_id"]
    assert selected is not None

    assert select_technician(
        persistence,
        entities=entities,
        start_at=ORIGIN + timedelta(hours=1, minutes=30),
        end_at=ORIGIN + timedelta(hours=2, minutes=30),
    ) is None


def test_missing_part_blocks_execution_before_technician_capacity():
    persistence, entities, engine, backend = _runtime(seed_part=False)
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
    backend.run_until(ORIGIN + timedelta(hours=1))

    assert reserve_required_part(
        persistence, engine, backend, entities=entities
    ) is False
    assert reconcile_work_start(
        persistence,
        engine,
        backend,
        entities=entities,
        appointment_id_value=appointment.id,
    ) is False
    assert persistence.resource_demands() == ()
    assert persistence.resource_reservations() == ()


def test_no_access_preserves_visit_and_part_then_creates_replacement():
    persistence, entities, engine, backend = _runtime()
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
    visit = record_visit(
        persistence,
        engine,
        backend,
        entities=entities,
        appointment_id_value=first.id,
        sequence=1,
        outcome="no_access",
    )

    work_order = persistence.entity("field_work_order", entities.work_order_id)
    first = persistence.entity("field_appointment", first.id)
    assert visit.state == "committed"
    assert first is not None and first.state == "no_access_recorded"
    assert work_order is not None and work_order.state == "reschedule_required"
    assert len(persistence.store_get_results()) == 1

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
    assert replacement.attributes["replaces_appointment_id"] == first.id
    assert replacement.state == "confirmed"
    assert len(persistence.store_get_results()) == 1


def test_visit_identity_cannot_change_outcome():
    persistence, entities, engine, backend = _runtime()
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
    backend.run_until(ORIGIN + timedelta(hours=1))
    assert reconcile_work_start(
        persistence,
        engine,
        backend,
        entities=entities,
        appointment_id_value=appointment.id,
    )
    record_visit(
        persistence,
        engine,
        backend,
        entities=entities,
        appointment_id_value=appointment.id,
        sequence=7,
        outcome="no_access",
    )
    persisted = persistence.entity(
        "field_visit_occurrence",
        visit_id(appointment.id, 7),
    )
    assert persisted is not None and persisted.state == "committed"
    with pytest.raises(ValueError, match="different outcome"):
        record_visit(
            persistence,
            engine,
            backend,
            entities=entities,
            appointment_id_value=appointment.id,
            sequence=7,
            outcome="completed",
        )


def test_second_active_appointment_is_rejected_even_if_preproposed():
    persistence, entities, engine, _ = _runtime()
    first = propose_appointment(
        persistence,
        engine,
        entities=entities,
        ordinal=1,
        start_at=ORIGIN + timedelta(hours=1),
        end_at=ORIGIN + timedelta(hours=2),
    )
    second = propose_appointment(
        persistence,
        engine,
        entities=entities,
        ordinal=2,
        start_at=ORIGIN + timedelta(hours=3),
        end_at=ORIGIN + timedelta(hours=4),
    )
    confirm_appointment(
        persistence,
        engine,
        entities=entities,
        appointment_id_value=first.id,
    )

    with pytest.raises(RuntimeError, match="active appointment"):
        confirm_appointment(
            persistence,
            engine,
            entities=entities,
            appointment_id_value=second.id,
        )
    with pytest.raises(RuntimeError, match="schedulable work order"):
        propose_appointment(
            persistence,
            engine,
            entities=entities,
            ordinal=3,
            start_at=ORIGIN + timedelta(hours=5),
            end_at=ORIGIN + timedelta(hours=6),
        )


def test_part_selection_filters_before_durable_ownership():
    persistence, entities, engine, backend = _runtime(seed_part=False)
    engine.stores.put(
        backend,
        store_name="field_parts",
        item_id="wrong-part-1",
        value={"sku": "copper-modem", "serial": "COPPER-1"},
        requested_at=backend.now,
    )
    engine.stores.put(
        backend,
        store_name="field_parts",
        item_id="ont-router-1",
        value={"sku": REQUIRED_PART, "serial": "ONT-0001"},
        requested_at=backend.now,
    )
    backend.run_until(backend.now)

    assert reserve_required_part(
        persistence,
        engine,
        backend,
        entities=entities,
    )
    results = persistence.store_get_results()
    assert len(results) == 1
    assert results[0].item.value["sku"] == REQUIRED_PART
    remaining = {item.item_id for item in persistence.store_items()}
    assert "wrong-part-1" in remaining
    assert "ont-router-1" not in remaining


def test_window_expiry_reconciles_started_work_and_technician():
    persistence, entities, engine, backend = _runtime()
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
    backend.run_until(ORIGIN + timedelta(hours=1))
    assert reconcile_work_start(
        persistence,
        engine,
        backend,
        entities=entities,
        appointment_id_value=appointment.id,
    )

    technician_id = str(appointment.attributes["technician_id"])
    technician = persistence.entity("field_technician", technician_id)
    assert technician is not None and technician.state == "assigned"
    assert persistence.resource_reservations() == ()

    backend.run_until(ORIGIN + timedelta(hours=2))

    work_order = persistence.entity("field_work_order", entities.work_order_id)
    appointment = persistence.entity("field_appointment", appointment.id)
    technician = persistence.entity("field_technician", technician_id)
    assert work_order is not None and work_order.state == "reschedule_required"
    assert appointment is not None and appointment.state == "no_access_recorded"
    assert technician is not None and technician.state == "available"
    assert persistence.resource_reservations() == ()
    assert persistence.scheduled_work() == ()


def test_window_expiry_reconciles_work_that_never_started():
    persistence, entities, engine, backend = _runtime()
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

    backend.run_until(ORIGIN + timedelta(hours=2))

    work_order = persistence.entity("field_work_order", entities.work_order_id)
    appointment = persistence.entity("field_appointment", appointment.id)
    technician = persistence.entity(
        "field_technician",
        str(appointment.attributes["technician_id"]),
    )
    assert work_order is not None and work_order.state == "reschedule_required"
    assert appointment is not None and appointment.state == "no_access_recorded"
    assert technician is not None and technician.state == "available"
    assert persistence.resource_demands() == ()
    assert persistence.resource_reservations() == ()
    assert persistence.scheduled_work() == ()

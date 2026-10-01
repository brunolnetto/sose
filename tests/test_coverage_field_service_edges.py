from __future__ import annotations

from datetime import timedelta

import pytest

from sose.backends.simpy import SimPyBackend
from sose.examples.field_service.scenarios import ORIGIN
from sose.examples.field_service.simulation import (
    _entity,
    build_runtime,
    confirm_appointment,
    propose_appointment,
    record_visit,
    reconcile_work_start,
    seed_part_inventory,
    seed_reference,
    select_technician,
)
from sose.persistence.memory import MemoryPersistence


def _runtime(*, seed_part: bool = True):
    persistence = MemoryPersistence()
    entities = seed_reference(persistence)
    _, engine = build_runtime(persistence)
    backend = SimPyBackend(origin=ORIGIN)
    engine.rebuild_backend(backend)
    if seed_part:
        seed_part_inventory(persistence, engine, backend)
    return persistence, entities, engine, backend


def _save(persistence, entity):
    with persistence.transaction() as uow:
        uow.save_entity(entity)


def _confirmed_window(persistence, entities, engine):
    appointment = propose_appointment(
        persistence,
        engine,
        entities=entities,
        ordinal=1,
        start_at=ORIGIN + timedelta(hours=1),
        end_at=ORIGIN + timedelta(hours=2),
    )
    return confirm_appointment(
        persistence,
        engine,
        entities=entities,
        appointment_id_value=appointment.id,
    )


def _started_boundary(persistence, entities, engine, backend):
    appointment = _confirmed_window(persistence, entities, engine)
    backend.run_until(ORIGIN + timedelta(hours=1))
    current = persistence.entity("field_appointment", appointment.id)
    assert current is not None and current.state == "in_progress"
    return current


def test_missing_field_service_entity_guard_is_observable():
    with pytest.raises(RuntimeError, match="was not persisted"):
        _entity(MemoryPersistence(), "field_work_order", "missing")


def test_propose_appointment_requires_positive_ordinal():
    persistence, entities, engine, _ = _runtime()

    with pytest.raises(ValueError, match="ordinal must be positive"):
        propose_appointment(
            persistence,
            engine,
            entities=entities,
            ordinal=0,
            start_at=ORIGIN + timedelta(hours=1),
            end_at=ORIGIN + timedelta(hours=2),
        )


def test_appointment_identity_replay_returns_existing_without_side_effects():
    persistence, entities, engine, _ = _runtime()
    start_at = ORIGIN + timedelta(hours=1)
    end_at = ORIGIN + timedelta(hours=2)
    first = propose_appointment(
        persistence,
        engine,
        entities=entities,
        ordinal=1,
        start_at=start_at,
        end_at=end_at,
    )
    before = tuple(persistence.entities())

    second = propose_appointment(
        persistence,
        engine,
        entities=entities,
        ordinal=1,
        start_at=start_at,
        end_at=end_at,
    )

    assert second == first
    assert tuple(persistence.entities()) == before


def test_appointment_identity_cannot_replay_with_different_window():
    persistence, entities, engine, _ = _runtime()
    first = propose_appointment(
        persistence,
        engine,
        entities=entities,
        ordinal=1,
        start_at=ORIGIN + timedelta(hours=1),
        end_at=ORIGIN + timedelta(hours=2),
    )

    with pytest.raises(ValueError, match="different window"):
        propose_appointment(
            persistence,
            engine,
            entities=entities,
            ordinal=1,
            start_at=ORIGIN + timedelta(hours=1),
            end_at=ORIGIN + timedelta(hours=3),
        )

    persisted = persistence.entity("field_appointment", first.id)
    assert persisted is not None
    assert persisted.attributes["end_at"] == (ORIGIN + timedelta(hours=2)).isoformat()


def test_terminal_stale_active_appointment_is_cleared_before_replacement():
    persistence, entities, engine, _ = _runtime()
    first = _confirmed_window(persistence, entities, engine)

    first.state = "completed"
    _save(persistence, first)
    work_order = persistence.entity("field_work_order", entities.work_order_id)
    assert work_order is not None
    work_order.state = "reschedule_required"
    assert work_order.attributes["active_appointment_id"] == first.id
    _save(persistence, work_order)

    replacement = propose_appointment(
        persistence,
        engine,
        entities=entities,
        ordinal=2,
        start_at=ORIGIN + timedelta(hours=3),
        end_at=ORIGIN + timedelta(hours=4),
        replaces_appointment_id=first.id,
    )

    work_order = persistence.entity("field_work_order", entities.work_order_id)
    assert work_order is not None
    assert work_order.attributes["active_appointment_id"] is None
    assert replacement.attributes["replaces_appointment_id"] == first.id


def test_missing_stale_active_appointment_id_is_recovered_before_proposal():
    persistence, entities, engine, _ = _runtime()
    work_order = persistence.entity("field_work_order", entities.work_order_id)
    assert work_order is not None
    work_order.attributes["active_appointment_id"] = "missing-appointment"
    _save(persistence, work_order)

    appointment = propose_appointment(
        persistence,
        engine,
        entities=entities,
        ordinal=1,
        start_at=ORIGIN + timedelta(hours=1),
        end_at=ORIGIN + timedelta(hours=2),
    )

    recovered = persistence.entity("field_work_order", entities.work_order_id)
    assert recovered is not None
    assert recovered.attributes["active_appointment_id"] is None
    assert appointment.state == "proposed"


def test_confirm_appointment_requires_skill_territory_time_eligible_technician():
    persistence, entities, engine, _ = _runtime()
    work_order = persistence.entity("field_work_order", entities.work_order_id)
    assert work_order is not None
    work_order.attributes["territory"] = "unserved"
    _save(persistence, work_order)

    appointment = propose_appointment(
        persistence,
        engine,
        entities=entities,
        ordinal=1,
        start_at=ORIGIN + timedelta(hours=1),
        end_at=ORIGIN + timedelta(hours=2),
    )

    assert select_technician(
        persistence,
        entities=entities,
        start_at=ORIGIN + timedelta(hours=1),
        end_at=ORIGIN + timedelta(hours=2),
    ) is None
    with pytest.raises(RuntimeError, match="no skill/territory/time eligible technician"):
        confirm_appointment(
            persistence,
            engine,
            entities=entities,
            appointment_id_value=appointment.id,
        )


def test_work_start_rejects_appointment_that_has_not_started():
    persistence, entities, engine, backend = _runtime()
    appointment = _confirmed_window(persistence, entities, engine)

    assert reconcile_work_start(
        persistence,
        engine,
        backend,
        entities=entities,
        appointment_id_value=appointment.id,
    ) is False


def test_work_start_requires_active_appointment_ownership():
    persistence, entities, engine, backend = _runtime()
    appointment = _started_boundary(persistence, entities, engine, backend)
    work_order = persistence.entity("field_work_order", entities.work_order_id)
    assert work_order is not None
    work_order.attributes["active_appointment_id"] = "other"
    _save(persistence, work_order)

    assert reconcile_work_start(
        persistence,
        engine,
        backend,
        entities=entities,
        appointment_id_value=appointment.id,
    ) is False


def test_work_start_requires_scheduled_work_order():
    persistence, entities, engine, backend = _runtime()
    appointment = _started_boundary(persistence, entities, engine, backend)
    work_order = persistence.entity("field_work_order", entities.work_order_id)
    assert work_order is not None
    work_order.state = "reschedule_required"
    _save(persistence, work_order)

    assert reconcile_work_start(
        persistence,
        engine,
        backend,
        entities=entities,
        appointment_id_value=appointment.id,
    ) is False


def test_work_start_respects_dispatch_outage(monkeypatch):
    persistence, entities, engine, backend = _runtime()
    appointment = _started_boundary(persistence, entities, engine, backend)
    monkeypatch.setattr(
        engine.context.scenarios,
        "attribute",
        lambda name, default=True: False
        if name == "field_service.dispatch.available"
        else default,
    )

    assert reconcile_work_start(
        persistence,
        engine,
        backend,
        entities=entities,
        appointment_id_value=appointment.id,
    ) is False
    assert persistence.resource_demands() == ()


def test_work_start_rejects_missing_technician_after_part_ownership():
    persistence, entities, engine, backend = _runtime()
    appointment = _started_boundary(persistence, entities, engine, backend)
    appointment.attributes["technician_id"] = None
    _save(persistence, appointment)

    with pytest.raises(RuntimeError, match="appointment has no technician"):
        reconcile_work_start(
            persistence,
            engine,
            backend,
            entities=entities,
            appointment_id_value=appointment.id,
        )
    assert len(persistence.store_get_results()) == 1


def test_work_start_waits_when_selected_technician_capacity_is_contended():
    persistence, entities, engine, backend = _runtime()
    appointment = _started_boundary(persistence, entities, engine, backend)
    technician_id = str(appointment.attributes["technician_id"])

    blocker = engine.resources.ensure_requested(
        backend,
        resource_name=f"field-tech:{technician_id}",
        request_id="field-tech:blocker",
        requested_at=backend.now,
    )
    assert blocker is not None

    assert reconcile_work_start(
        persistence,
        engine,
        backend,
        entities=entities,
        appointment_id_value=appointment.id,
    ) is False
    request_id = f"field-tech:{appointment.id}"
    assert any(
        demand.request_id == request_id
        for demand in persistence.resource_demands()
    )
    assert engine.resources.reservation_for(request_id) is None
    assert engine.resources.reservation_for("field-tech:blocker") == blocker


@pytest.mark.parametrize(
    ("sequence", "outcome", "message"),
    [
        (0, "completed", "visit sequence must be positive"),
        (1, "unknown", "unsupported visit outcome"),
    ],
)
def test_visit_validates_sequence_and_outcome(sequence, outcome, message):
    persistence, entities, engine, backend = _runtime()
    appointment = _confirmed_window(persistence, entities, engine)

    with pytest.raises(ValueError, match=message):
        record_visit(
            persistence,
            engine,
            backend,
            entities=entities,
            appointment_id_value=appointment.id,
            sequence=sequence,
            outcome=outcome,
        )


def test_visit_requires_work_order_in_progress_even_when_appointment_started():
    persistence, entities, engine, backend = _runtime()
    appointment = _started_boundary(persistence, entities, engine, backend)

    with pytest.raises(RuntimeError, match="requires active appointment and work order"):
        record_visit(
            persistence,
            engine,
            backend,
            entities=entities,
            appointment_id_value=appointment.id,
            sequence=1,
            outcome="completed",
        )


def test_visit_requires_appointment_in_progress_even_when_work_order_started():
    persistence, entities, engine, backend = _runtime()
    appointment = _started_boundary(persistence, entities, engine, backend)
    assert reconcile_work_start(
        persistence,
        engine,
        backend,
        entities=entities,
        appointment_id_value=appointment.id,
    )

    appointment = persistence.entity("field_appointment", appointment.id)
    assert appointment is not None
    appointment.state = "confirmed"
    _save(persistence, appointment)
    work_order = persistence.entity("field_work_order", entities.work_order_id)
    assert work_order is not None and work_order.state == "in_progress"

    with pytest.raises(RuntimeError, match="requires active appointment and work order"):
        record_visit(
            persistence,
            engine,
            backend,
            entities=entities,
            appointment_id_value=appointment.id,
            sequence=2,
            outcome="completed",
        )


def test_visit_replay_with_same_outcome_is_idempotent_after_completion():
    persistence, entities, engine, backend = _runtime()
    appointment = _started_boundary(persistence, entities, engine, backend)
    assert reconcile_work_start(
        persistence,
        engine,
        backend,
        entities=entities,
        appointment_id_value=appointment.id,
    )

    first = record_visit(
        persistence,
        engine,
        backend,
        entities=entities,
        appointment_id_value=appointment.id,
        sequence=1,
        outcome="completed",
    )
    events_before = tuple(persistence.events())

    second = record_visit(
        persistence,
        engine,
        backend,
        entities=entities,
        appointment_id_value=appointment.id,
        sequence=1,
        outcome="completed",
    )

    assert second == first
    assert tuple(persistence.events()) == events_before

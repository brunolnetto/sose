from __future__ import annotations

import pytest

from sose.backends.simpy import SimPyBackend
from sose.examples.aviation.simulation import (
    ORIGIN,
    _entity,
    build_runtime,
    complete_aog_maintenance,
    ensure_inspection,
    ensure_maintenance,
    reconcile_aog_maintenance,
    reconcile_departure,
    reconcile_inspection,
    reconcile_part_issue,
    seed_reference,
    seed_spare_part,
    land_flight,
    maintenance_work_order_id,
)
from sose.persistence.memory import MemoryPersistence


def _runtime():
    persistence = MemoryPersistence()
    entities = seed_reference(persistence)
    _, engine = build_runtime(persistence)
    backend = SimPyBackend(origin=ORIGIN)
    engine.rebuild_backend(backend)
    return persistence, entities, engine, backend


def _set(persistence, entity_type: str, entity_id: str, state: str):
    with persistence.transaction() as uow:
        entity = uow.get_entity(entity_type, entity_id)
        assert entity is not None
        entity.state = state
        uow.save_entity(entity)
    return persistence.entity(entity_type, entity_id)


def _prepare_inspection(persistence, entities, engine):
    _set(persistence, "aviation_flight", entities.leg1_id, "inspection")
    inspection = ensure_inspection(
        persistence,
        engine,
        flight_id=entities.leg1_id,
    )
    return inspection


def _prepare_maintenance(persistence, entities, engine, backend):
    work, demand = ensure_maintenance(
        persistence,
        engine,
        backend,
        entities=entities,
        flight_id=entities.leg1_id,
    )
    return work, demand


def test_airborne_departure_reconciles_assigned_aircraft():
    persistence, entities, engine, backend = _runtime()
    _set(persistence, "aviation_flight", entities.leg1_id, "airborne")
    _set(persistence, "aviation_aircraft", entities.aircraft_id, "assigned")

    assert reconcile_departure(
        persistence,
        engine,
        backend,
        entities=entities,
        flight_id=entities.leg1_id,
    )
    assert (
        persistence.entity("aviation_aircraft", entities.aircraft_id).state
        == "airborne"
    )


def test_airborne_departure_is_idempotent_when_aircraft_is_already_airborne():
    persistence, entities, engine, backend = _runtime()
    _set(persistence, "aviation_flight", entities.leg1_id, "airborne")
    _set(persistence, "aviation_aircraft", entities.aircraft_id, "airborne")

    assert reconcile_departure(
        persistence,
        engine,
        backend,
        entities=entities,
        flight_id=entities.leg1_id,
    )


def test_delayed_departure_stays_delayed_when_departure_is_unavailable(monkeypatch):
    persistence, entities, engine, backend = _runtime()
    _set(persistence, "aviation_flight", entities.leg1_id, "delayed")
    monkeypatch.setattr(
        engine.context.scenarios,
        "attribute",
        lambda name, default=True: False
        if name == "aviation.departure.available"
        else default,
    )

    assert (
        reconcile_departure(
            persistence,
            engine,
            backend,
            entities=entities,
            flight_id=entities.leg1_id,
        )
        is False
    )
    assert (
        persistence.entity("aviation_flight", entities.leg1_id).state
        == "delayed"
    )


def test_ready_departure_with_active_crew_skips_redundant_transitions():
    persistence, entities, engine, backend = _runtime()
    _set(persistence, "aviation_flight", entities.leg1_id, "ready")
    _set(
        persistence,
        "aviation_crew_assignment",
        entities.leg1_crew_id,
        "active",
    )

    assert reconcile_departure(
        persistence,
        engine,
        backend,
        entities=entities,
        flight_id=entities.leg1_id,
    )
    assert persistence.entity("aviation_flight", entities.leg1_id).state == "airborne"


def test_landing_recovery_accepts_already_inspected_aircraft_and_released_crew():
    persistence, entities, engine, backend = _runtime()
    _set(persistence, "aviation_flight", entities.leg1_id, "inspection")
    _set(persistence, "aviation_aircraft", entities.aircraft_id, "inspection")
    _set(
        persistence,
        "aviation_crew_assignment",
        entities.leg1_crew_id,
        "released",
    )

    inspection = land_flight(
        persistence,
        engine,
        backend,
        entities=entities,
        flight_id=entities.leg1_id,
    )

    assert inspection.state == "pending"
    assert persistence.entity("aviation_flight", entities.leg1_id).state == "inspection"


def test_inspection_recovery_from_inspecting_skips_begin():
    persistence, entities, engine, backend = _runtime()
    inspection = _prepare_inspection(persistence, entities, engine)
    _set(persistence, "aviation_aircraft", entities.aircraft_id, "inspection")
    _set(persistence, "aviation_inspection", inspection.id, "inspecting")

    assert reconcile_inspection(
        persistence,
        engine,
        backend,
        entities=entities,
        flight_id=entities.leg1_id,
    )
    assert _entity(persistence, "aviation_inspection", inspection.id).state == "passed"


def test_passed_inspection_is_idempotent_after_aircraft_and_flight_release():
    persistence, entities, engine, backend = _runtime()
    inspection = _prepare_inspection(persistence, entities, engine)
    _set(persistence, "aviation_inspection", inspection.id, "passed")
    _set(persistence, "aviation_aircraft", entities.aircraft_id, "released")
    _set(persistence, "aviation_flight", entities.leg1_id, "released")

    assert reconcile_inspection(
        persistence,
        engine,
        backend,
        entities=entities,
        flight_id=entities.leg1_id,
    )


def test_failed_inspection_does_not_remark_already_aog_aircraft():
    persistence, entities, engine, backend = _runtime()
    inspection = _prepare_inspection(persistence, entities, engine)
    _set(persistence, "aviation_inspection", inspection.id, "failed")
    _set(persistence, "aviation_aircraft", entities.aircraft_id, "aog")

    assert (
        reconcile_inspection(
            persistence,
            engine,
            backend,
            entities=entities,
            flight_id=entities.leg1_id,
        )
        is False
    )
    assert persistence.entity(
        "aviation_maintenance_work_order",
        maintenance_work_order_id(entities.leg1_id),
    ) is not None


def test_already_issued_part_without_waiting_work_is_idempotent():
    persistence, entities, engine, backend = _runtime()
    work, demand = _prepare_maintenance(persistence, entities, engine, backend)
    _set(persistence, "aviation_part_demand", demand.id, "issued")

    assert reconcile_part_issue(
        persistence,
        engine,
        backend,
        entities=entities,
        flight_id=entities.leg1_id,
    )
    assert _entity(
        persistence,
        "aviation_maintenance_work_order",
        work.id,
    ).state == "released"


def test_part_issue_reuses_preexisting_store_selection():
    persistence, entities, engine, backend = _runtime()
    work, demand = _prepare_maintenance(persistence, entities, engine, backend)
    seed_spare_part(persistence, engine, backend, item_id="preselected-part")

    result_id = f"part-issue:{demand.id}"
    selected = engine.stores.ensure_selection(
        backend,
        store_name="part_lots",
        request_id=result_id,
        requested_at=backend.now,
    )
    assert selected is not None

    assert reconcile_part_issue(
        persistence,
        engine,
        backend,
        entities=entities,
        flight_id=entities.leg1_id,
    )
    assert _entity(persistence, "aviation_part_demand", demand.id).state == "issued"
    assert _entity(
        persistence,
        "aviation_maintenance_work_order",
        work.id,
    ).state == "released"


def test_part_issue_accepts_preallocated_demand_with_existing_selection():
    persistence, entities, engine, backend = _runtime()
    _, demand = _prepare_maintenance(persistence, entities, engine, backend)
    seed_spare_part(persistence, engine, backend, item_id="allocated-part")
    selected = engine.stores.ensure_selection(
        backend,
        store_name="part_lots",
        request_id=f"part-issue:{demand.id}",
        requested_at=backend.now,
    )
    assert selected is not None
    _set(persistence, "aviation_part_demand", demand.id, "allocated")

    assert reconcile_part_issue(
        persistence,
        engine,
        backend,
        entities=entities,
        flight_id=entities.leg1_id,
    )
    assert _entity(persistence, "aviation_part_demand", demand.id).state == "issued"


def test_part_issue_rejects_corrupted_demand_state_instead_of_recursing():
    persistence, entities, engine, backend = _runtime()
    _, demand = _prepare_maintenance(persistence, entities, engine, backend)
    seed_spare_part(persistence, engine, backend, item_id="corrupt-part")
    selected = engine.stores.ensure_selection(
        backend,
        store_name="part_lots",
        request_id=f"part-issue:{demand.id}",
        requested_at=backend.now,
    )
    assert selected is not None
    _set(persistence, "aviation_part_demand", demand.id, "corrupted")

    with pytest.raises(RuntimeError, match="cannot reconcile issue from corrupted"):
        reconcile_part_issue(
            persistence,
            engine,
            backend,
            entities=entities,
            flight_id=entities.leg1_id,
        )


def test_part_issue_handles_selection_race_after_inventory_snapshot(monkeypatch):
    persistence, entities, engine, backend = _runtime()
    _, demand = _prepare_maintenance(persistence, entities, engine, backend)
    seed_spare_part(persistence, engine, backend, item_id="raced-part")
    monkeypatch.setattr(engine.stores, "ensure_selection", lambda *args, **kwargs: None)

    assert (
        reconcile_part_issue(
            persistence,
            engine,
            backend,
            entities=entities,
            flight_id=entities.leg1_id,
        )
        is False
    )
    assert _entity(persistence, "aviation_part_demand", demand.id).state == "open"


def test_aog_maintenance_waits_when_another_queue_item_has_priority():
    persistence, entities, engine, backend = _runtime()
    work, demand = _prepare_maintenance(persistence, entities, engine, backend)
    _set(persistence, "aviation_part_demand", demand.id, "issued")
    engine.stores.put(
        backend,
        store_name="maintenance_queue",
        item_id="priority-blocker",
        value={"work_order_id": "other"},
        priority=0,
        requested_at=backend.now,
    )
    backend.run_until(backend.now)

    assert (
        reconcile_aog_maintenance(
            persistence,
            engine,
            backend,
            entities=entities,
            flight_id=entities.leg1_id,
        )
        is False
    )
    assert _entity(
        persistence,
        "aviation_maintenance_work_order",
        work.id,
    ).state == "waiting_bay"


def test_aog_maintenance_handles_queue_selection_race(monkeypatch):
    persistence, entities, engine, backend = _runtime()
    work, demand = _prepare_maintenance(persistence, entities, engine, backend)
    _set(persistence, "aviation_part_demand", demand.id, "issued")
    monkeypatch.setattr(engine.stores, "ensure_selection", lambda *args, **kwargs: None)

    assert (
        reconcile_aog_maintenance(
            persistence,
            engine,
            backend,
            entities=entities,
            flight_id=entities.leg1_id,
        )
        is False
    )
    assert _entity(
        persistence,
        "aviation_maintenance_work_order",
        work.id,
    ).state == "waiting_bay"


def test_aog_maintenance_waits_when_higher_priority_bay_owner_blocks_it():
    persistence, entities, engine, backend = _runtime()
    work, demand = _prepare_maintenance(persistence, entities, engine, backend)
    _set(persistence, "aviation_part_demand", demand.id, "issued")
    engine.preemptive_resources.request(
        backend,
        resource_name="maintenance_bay",
        request_id="bay-blocker",
        requested_at=backend.now,
        priority=0,
        preempt=False,
    )
    backend.run_until(backend.now)

    assert (
        reconcile_aog_maintenance(
            persistence,
            engine,
            backend,
            entities=entities,
            flight_id=entities.leg1_id,
        )
        is False
    )
    assert engine.preemptive_resources.reservation_for(
        f"maintenance-bay:{work.id}"
    ) is None


def test_aog_maintenance_recovery_accepts_in_progress_work_and_aircraft():
    persistence, entities, engine, backend = _runtime()
    work, demand = _prepare_maintenance(persistence, entities, engine, backend)
    _set(persistence, "aviation_part_demand", demand.id, "issued")
    _set(
        persistence,
        "aviation_maintenance_work_order",
        work.id,
        "in_progress",
    )
    _set(persistence, "aviation_aircraft", entities.aircraft_id, "maintenance")

    assert reconcile_aog_maintenance(
        persistence,
        engine,
        backend,
        entities=entities,
        flight_id=entities.leg1_id,
    )


def test_completed_maintenance_is_idempotent_when_rotation_already_released():
    persistence, entities, engine, backend = _runtime()
    work, _ = _prepare_maintenance(persistence, entities, engine, backend)
    _set(
        persistence,
        "aviation_maintenance_work_order",
        work.id,
        "completed",
    )
    _set(persistence, "aviation_aircraft", entities.aircraft_id, "released")
    _set(persistence, "aviation_flight", entities.leg1_id, "released")

    assert complete_aog_maintenance(
        persistence,
        engine,
        backend,
        entities=entities,
        flight_id=entities.leg1_id,
    )


def test_completed_work_requires_aircraft_release_evidence():
    persistence, entities, engine, backend = _runtime()
    work, _ = _prepare_maintenance(persistence, entities, engine, backend)
    _set(
        persistence,
        "aviation_maintenance_work_order",
        work.id,
        "completed",
    )
    _set(persistence, "aviation_aircraft", entities.aircraft_id, "aog")

    with pytest.raises(RuntimeError, match="did not release aircraft"):
        complete_aog_maintenance(
            persistence,
            engine,
            backend,
            entities=entities,
            flight_id=entities.leg1_id,
        )

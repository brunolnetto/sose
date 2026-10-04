from __future__ import annotations

from datetime import timedelta

import pytest

from sose.backends.simpy import SimPyBackend
from sose.examples.aviation.simulation import (
    ORIGIN,
    _crew_for_flight,
    _entity,
    build_runtime,
    complete_aog_maintenance,
    ensure_inspection,
    ensure_maintenance,
    land_flight,
    maintenance_work_order_id,
    part_demand_id,
    reconcile_aog_maintenance,
    reconcile_departure,
    reconcile_inspection,
    reconcile_part_issue,
    schedule_departure,
    seed_reference,
    seed_spare_part,
)
from sose.persistence.memory import MemoryPersistence


def _runtime():
    persistence = MemoryPersistence()
    entities = seed_reference(persistence)
    _, engine = build_runtime(persistence)
    backend = SimPyBackend(origin=ORIGIN)
    engine.rebuild_backend(backend)
    return persistence, entities, engine, backend


def _save(persistence, entity):
    with persistence.transaction() as uow:
        uow.save_entity(entity)


def test_missing_aviation_entity_guard_is_observable():
    with pytest.raises(RuntimeError, match="was not persisted"):
        _entity(MemoryPersistence(), "aviation_flight", "missing")


def test_departure_schedule_is_idempotent_and_terminal_flight_returns_now():
    persistence, entities, engine, backend = _runtime()

    first = schedule_departure(
        persistence,
        engine,
        backend,
        flight_id=entities.leg1_id,
        delay=timedelta(hours=1),
    )
    second = schedule_departure(
        persistence,
        engine,
        backend,
        flight_id=entities.leg1_id,
        delay=timedelta(hours=2),
    )
    assert second == first
    assert len(persistence.scheduled_work()) == 1

    flight = persistence.entity("aviation_flight", entities.leg2_id)
    assert flight is not None
    flight.state = "released"
    _save(persistence, flight)

    assert schedule_departure(
        persistence,
        engine,
        backend,
        flight_id=entities.leg2_id,
        delay=timedelta(hours=10),
    ) == backend.now
    assert len(persistence.scheduled_work()) == 1


def test_unknown_reference_flight_has_no_crew_mapping():
    persistence, entities, _, _ = _runtime()
    assert persistence.entity("aviation_flight", entities.leg1_id) is not None

    with pytest.raises(KeyError, match="unknown reference flight"):
        _crew_for_flight(entities, "unknown")


def test_departure_returns_false_outside_departure_states():
    persistence, entities, engine, backend = _runtime()

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


def test_departure_outage_moves_due_flight_to_delayed_without_crew_demand(monkeypatch):
    persistence, entities, engine, backend = _runtime()
    flight = persistence.entity("aviation_flight", entities.leg1_id)
    assert flight is not None
    flight.state = "due"
    _save(persistence, flight)

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
    persisted = persistence.entity("aviation_flight", entities.leg1_id)
    assert persisted is not None and persisted.state == "delayed"
    request_id = f"flight-crew:{entities.leg1_id}"
    assert engine.resources.has_request(request_id) is False


def test_landing_rejects_invalid_flight_state():
    persistence, entities, engine, backend = _runtime()

    with pytest.raises(RuntimeError, match="flight cannot reconcile landing"):
        land_flight(
            persistence,
            engine,
            backend,
            entities=entities,
            flight_id=entities.leg1_id,
        )


def test_landing_rejects_invalid_aircraft_state_after_airborne_flight():
    persistence, entities, engine, backend = _runtime()
    flight = persistence.entity("aviation_flight", entities.leg1_id)
    aircraft = persistence.entity("aviation_aircraft", entities.aircraft_id)
    assert flight is not None and aircraft is not None
    flight.state = "airborne"
    aircraft.state = "available"
    _save(persistence, flight)
    _save(persistence, aircraft)

    with pytest.raises(RuntimeError, match="aircraft cannot reconcile landing"):
        land_flight(
            persistence,
            engine,
            backend,
            entities=entities,
            flight_id=entities.leg1_id,
        )


def test_inspection_requires_flight_inspection_state_and_creation_is_idempotent():
    persistence, entities, engine, _ = _runtime()

    with pytest.raises(RuntimeError, match=r"requires Flight\(inspection\)"):
        ensure_inspection(
            persistence,
            engine,
            flight_id=entities.leg1_id,
        )

    flight = persistence.entity("aviation_flight", entities.leg1_id)
    assert flight is not None
    flight.state = "inspection"
    _save(persistence, flight)

    first = ensure_inspection(
        persistence,
        engine,
        flight_id=entities.leg1_id,
    )
    count_before = len(persistence.entities())
    second = ensure_inspection(
        persistence,
        engine,
        flight_id=entities.leg1_id,
    )

    assert second.id == first.id
    assert len(persistence.entities()) == count_before


def test_inspection_waits_for_team_capacity():
    persistence, entities, engine, backend = _runtime()
    flight = persistence.entity("aviation_flight", entities.leg1_id)
    aircraft = persistence.entity("aviation_aircraft", entities.aircraft_id)
    assert flight is not None and aircraft is not None
    flight.state = "inspection"
    aircraft.state = "inspection"
    _save(persistence, flight)
    _save(persistence, aircraft)
    inspection = ensure_inspection(
        persistence,
        engine,
        flight_id=entities.leg1_id,
    )

    blocker = engine.resources.ensure_requested(
        backend,
        resource_name="inspection_team",
        request_id="inspection:blocker",
        requested_at=backend.now,
    )
    assert blocker is not None

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
    persisted = persistence.entity("aviation_inspection", inspection.id)
    assert persisted is not None and persisted.state == "pending"
    request_id = f"inspection-team:{inspection.id}"
    assert engine.resources.has_request(request_id)
    assert engine.resources.reservation_for(request_id) is None


def test_seed_spare_part_is_idempotent():
    persistence, _, engine, backend = _runtime()

    seed_spare_part(
        persistence,
        engine,
        backend,
        item_id="lot-x",
    )
    items_before = tuple(persistence.store_items())
    seed_spare_part(
        persistence,
        engine,
        backend,
        item_id="lot-x",
    )

    assert tuple(persistence.store_items()) == items_before


def test_part_issue_reconciles_already_issued_demand_and_waiting_work():
    persistence, entities, engine, backend = _runtime()
    work, demand = ensure_maintenance(
        persistence,
        engine,
        backend,
        entities=entities,
        flight_id=entities.leg1_id,
    )
    work.state = "waiting_part"
    demand.state = "issued"
    _save(persistence, work)
    _save(persistence, demand)

    assert reconcile_part_issue(
        persistence,
        engine,
        backend,
        entities=entities,
        flight_id=entities.leg1_id,
    )
    persisted = persistence.entity("aviation_maintenance_work_order", work.id)
    assert persisted is not None and persisted.state == "released"


def test_aog_maintenance_waits_until_part_is_issued():
    persistence, entities, engine, backend = _runtime()
    work, demand = ensure_maintenance(
        persistence,
        engine,
        backend,
        entities=entities,
        flight_id=entities.leg1_id,
    )
    assert work.state == "released"
    assert demand.state == "open"

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
    request_id = f"maintenance-bay:{work.id}"
    assert engine.preemptive_resources.has_request(request_id) is False


def test_complete_aog_maintenance_rejects_inactive_work():
    persistence, entities, engine, backend = _runtime()
    work, _ = ensure_maintenance(
        persistence,
        engine,
        backend,
        entities=entities,
        flight_id=entities.leg1_id,
    )
    assert work.id == maintenance_work_order_id(entities.leg1_id)
    assert persistence.entity(
        "aviation_part_demand",
        part_demand_id(entities.leg1_id),
    ) is not None

    with pytest.raises(RuntimeError, match="not complete or active"):
        complete_aog_maintenance(
            persistence,
            engine,
            backend,
            entities=entities,
            flight_id=entities.leg1_id,
        )

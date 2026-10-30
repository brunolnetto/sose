"""#590: executable warehouse contention, ownership and recovery falsifiers.

These tests use the registered real Warehouse Management StateCharts, Engine,
MemoryPersistence and physically enumerated dock/forklift entities. Direct
record mutations are restricted to explicit corruption/recovery tests; they
are never claimed to be legitimate domain transitions or PostgreSQL proofs.
"""
from __future__ import annotations

from datetime import timedelta

import pytest

from sose.examples.warehouse_management import simulation as wm
from sose.persistence.memory import MemoryPersistence


OWNER = "org:warehouse-capacity-coverage"


def _world(*, dock_count: int = 1, forklift_count: int = 1):
    store = MemoryPersistence()
    entities = wm.seed_reference(
        store, owner_id=OWNER, instance_key="coverage:warehouse-contention",
        dock_count=dock_count, forklift_count=forklift_count,
    )
    _, engine = wm.build_runtime(store, owner_id=OWNER)
    return store, entities, engine


def _get(store, kind, entity_id):
    value = store.entity(f"warehouse_management_{kind}", entity_id)
    assert value is not None
    return value


def _change(store, kind, identity, **attrs):
    with store.transaction() as uow:
        record = uow.get_entity(f"warehouse_management_{kind}", identity)
        assert record is not None
        record.attributes.update(attrs)
        uow.save_entity(record)


def _depart(store, entities, engine):
    assert wm.start_transfer(store, engine, entities=entities)
    assert _get(store, "shipment", entities.shipment_id).state == "in_transit"


def _dock(store, entities, engine):
    _depart(store, entities, engine)
    assert wm.arrive_truck(store, engine, entities=entities)
    assert _get(store, "shipment", entities.shipment_id).state == "docked"


def _handling(store, entities, engine):
    _dock(store, entities, engine)
    assert wm.assign_forklift(store, engine, entities=entities)
    assert _get(store, "shipment", entities.shipment_id).state == "handling"


def test_durable_foreign_stock_reservation_blocks_internal_transfer_without_debit():
    store, entities, engine = _world()
    assert wm.reserve_external_stock(
        store, engine, stock_id=entities.origin_stock_id,
        quantity=18.0, sku=wm.REFERENCE_SKU,
        reservation_reference="foreign:allocated",
    )
    before = _get(store, "stock", entities.origin_stock_id)
    events_before = store.events()
    assert wm.start_transfer(store, engine, entities=entities) is False
    assert _get(store, "stock", entities.origin_stock_id) == before
    assert store.events() == events_before
    assert _get(store, "shipment", entities.shipment_id).state == "planned"
    assert _get(store, "truck", entities.truck_id).state == "scheduled"


def test_repeated_departure_does_not_redebit_warehouse_stock():
    store, entities, engine = _world()
    _depart(store, entities, engine)
    before = _get(store, "stock", entities.origin_stock_id)
    events_before = store.events()
    assert wm.start_transfer(store, engine, entities=entities) is True
    assert _get(store, "stock", entities.origin_stock_id) == before
    assert store.events() == events_before


def test_in_transit_shipment_without_persisted_stock_reservation_is_rejected():
    store, entities, engine = _world()
    _depart(store, entities, engine)
    _change(store, "shipment", entities.shipment_id, stock_reserved=False)
    before = _get(store, "stock", entities.origin_stock_id)
    events_before = store.events()
    with pytest.raises(RuntimeError, match="missing durable stock reservation"):
        wm.start_transfer(store, engine, entities=entities)
    assert _get(store, "stock", entities.origin_stock_id) == before
    assert store.events() == events_before


def test_finite_dock_contention_enters_durable_wait_then_resumes_without_rearrival():
    store, entities, engine = _world()
    only_dock = entities.dock_ids[0]
    dock = _get(store, "dock", only_dock)
    wm._dispatch(engine, dock, "reserve", key=("coverage", only_dock, "block"))
    assert _get(store, "dock", only_dock).state == "reserved"

    _depart(store, entities, engine)
    engine.context.clock.advance(1)
    assert wm.arrive_truck(store, engine, entities=entities) is False
    delayed = _get(store, "shipment", entities.shipment_id)
    assert delayed.state == "delayed"
    assert delayed.attributes["arrived_at"] == engine.context.clock.now.isoformat()
    assert _get(store, "truck", entities.truck_id).state == "waiting_dock"
    assert delayed.attributes["assigned_dock_id"] is None
    events_before = store.events()

    # Repeated arrival cannot pretend a foreign-reserved dock is available.
    engine.context.clock.advance(1)
    assert wm.arrive_truck(store, engine, entities=entities) is False
    assert _get(store, "shipment", entities.shipment_id).attributes["arrived_at"] == delayed.attributes["arrived_at"]
    assert store.events() == events_before

    dock = _get(store, "dock", only_dock)
    wm._dispatch(engine, dock, "release", key=("coverage", only_dock, "unblock"))
    assert _get(store, "dock", only_dock).state == "available"
    assert wm.arrive_truck(store, engine, entities=entities) is True
    resumed = _get(store, "shipment", entities.shipment_id)
    assert resumed.state == "docked"
    assert resumed.attributes["assigned_dock_id"] == only_dock
    assert resumed.attributes["arrived_at"] == delayed.attributes["arrived_at"]
    assert _get(store, "dock", only_dock).state == "occupied"
    assert _get(store, "truck", entities.truck_id).state == "docked"


def test_multiple_named_docks_skip_foreign_reservation_instead_of_aliasing_capacity():
    store, entities, engine = _world(dock_count=2)
    first, second = entities.dock_ids
    wm._dispatch(
        engine, _get(store, "dock", first),
        "reserve", key=("coverage", first, "occupied-by-foreign"),
    )
    _depart(store, entities, engine)
    assert wm.arrive_truck(store, engine, entities=entities)
    shipment = _get(store, "shipment", entities.shipment_id)
    assert shipment.attributes["assigned_dock_id"] == second
    assert _get(store, "dock", first).state == "reserved"
    assert _get(store, "dock", second).state == "occupied"


def test_forklift_requires_docked_shipment_before_any_request():
    store, entities, engine = _world()
    events_before = store.events()
    assert wm.assign_forklift(store, engine, entities=entities) is False
    assert _get(store, "forklift", entities.forklift_ids[0]).state == "available"
    assert store.events() == events_before


def test_finite_forklift_contention_does_not_start_handling_before_release():
    store, entities, engine = _world()
    _dock(store, entities, engine)
    forklift_id = entities.forklift_ids[0]
    wm._dispatch(
        engine, _get(store, "forklift", forklift_id),
        "assign", key=("coverage", forklift_id, "foreign-work"),
    )
    events_before = store.events()
    assert wm.assign_forklift(store, engine, entities=entities) is False
    assert _get(store, "shipment", entities.shipment_id).state == "docked"
    assert _get(store, "shipment", entities.shipment_id).attributes["assigned_forklift_id"] is None
    assert store.events() == events_before

    wm._dispatch(
        engine, _get(store, "forklift", forklift_id),
        "release", key=("coverage", forklift_id, "foreign-work-released"),
    )
    assert wm.assign_forklift(store, engine, entities=entities) is True
    assert _get(store, "shipment", entities.shipment_id).state == "handling"
    assert _get(store, "shipment", entities.shipment_id).attributes["assigned_forklift_id"] == forklift_id


def test_multiple_named_forklifts_skip_foreign_assignment():
    store, entities, engine = _world(forklift_count=2)
    _dock(store, entities, engine)
    first, second = entities.forklift_ids
    wm._dispatch(
        engine, _get(store, "forklift", first),
        "assign", key=("coverage", first, "foreign-work"),
    )
    assert wm.assign_forklift(store, engine, entities=entities)
    shipment = _get(store, "shipment", entities.shipment_id)
    assert shipment.attributes["assigned_forklift_id"] == second
    assert _get(store, "forklift", first).state == "assigned"
    assert _get(store, "forklift", second).state == "assigned"


@pytest.mark.parametrize("missing", ("assigned_dock_id", "assigned_forklift_id"))
def test_unload_rejects_missing_durable_physical_owner_without_moving_inventory(missing):
    store, entities, engine = _world()
    _handling(store, entities, engine)
    _change(store, "shipment", entities.shipment_id, **{missing: None})
    before_origin = _get(store, "stock", entities.origin_stock_id)
    before_dest = _get(store, "stock", entities.destination_stock_id)
    events_before = store.events()
    with pytest.raises(RuntimeError, match="handling shipment is missing dock or forklift ownership"):
        wm.complete_unload(store, engine, entities=entities)
    assert _get(store, "stock", entities.origin_stock_id) == before_origin
    assert _get(store, "stock", entities.destination_stock_id) == before_dest
    assert store.events() == events_before


@pytest.mark.parametrize("missing", ("assigned_dock_id", "assigned_forklift_id"))
def test_completed_shipment_rejects_corrupt_release_authority(missing):
    store, entities, engine = _world()
    _handling(store, entities, engine)
    assert wm.complete_unload(store, engine, entities=entities) is True
    _change(store, "shipment", entities.shipment_id, resources_released=False, **{missing: None})
    events_before = store.events()
    with pytest.raises(RuntimeError, match="completed shipment is missing durable handling ownership"):
        wm._release_completed_resources(store, engine, entities=entities)
    assert store.events() == events_before


def test_completed_resource_release_checkpoint_can_reconstruct_exact_grants_without_extra_events():
    store, entities, engine = _world()
    _handling(store, entities, engine)
    assert wm.complete_unload(store, engine, entities=entities) is True
    before_stock = _get(store, "stock", entities.origin_stock_id)
    before_events = store.events()
    # Simulate crash before durable release marker; real resource states are already released.
    _change(store, "shipment", entities.shipment_id, resources_released=False)
    assert wm._release_completed_resources(store, engine, entities=entities) is True
    shipment = _get(store, "shipment", entities.shipment_id)
    assert shipment.attributes["resources_released"] is True
    assert _get(store, "stock", entities.origin_stock_id) == before_stock
    assert store.events() == before_events


def test_revisiting_handling_or_completed_steps_never_reissues_business_events():
    store, entities, engine = _world()
    _handling(store, entities, engine)
    before = store.events()
    # The only physical dock is already occupied by THIS shipment. Arrival
    # reports no *new* admission; it cannot double-reserve a second dock.
    assert wm.arrive_truck(store, engine, entities=entities) is False
    assert wm.assign_forklift(store, engine, entities=entities) is True
    assert store.events() == before

    assert wm.complete_unload(store, engine, entities=entities) is True
    completed_events = store.events()
    assert wm.arrive_truck(store, engine, entities=entities) is True
    assert wm.assign_forklift(store, engine, entities=entities) is False
    assert store.events() == completed_events

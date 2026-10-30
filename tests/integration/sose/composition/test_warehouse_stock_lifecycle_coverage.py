"""#590: real warehouse-owned inventory reservations and transfer lifecycle.

Use MemoryPersistence, the registered warehouse Engine/StateCharts and actual
DomainEvents. Neither mocks nor manually invented state transitions certify
the core positive flow. PostgreSQL concurrency/fencing remains a separate gate.
"""
from __future__ import annotations

import pytest

from sose.examples.warehouse_management import simulation as wm
from sose.persistence.memory import MemoryPersistence


OWNER = "org:wm-coverage"
SKU = wm.REFERENCE_SKU


def _world(*, available=20.0, transfer=8.0):
    store = MemoryPersistence()
    entities = wm.seed_reference(
        store, owner_id=OWNER, instance_key="coverage:stock-lifecycle",
        origin_on_hand=available, transfer_quantity=transfer,
        dock_count=1, forklift_count=1,
    )
    _, engine = wm.build_runtime(store, owner_id=OWNER)
    return store, entities, engine


def _stock(store, entities):
    return store.entity("warehouse_management_stock", entities.origin_stock_id)


def _reserve(store, entities, engine, **changes):
    args = dict(
        stock_id=entities.origin_stock_id, quantity=5.0,
        sku=SKU, reservation_reference="reservation:immutable",
    )
    args.update(changes)
    return wm.reserve_external_stock(store, engine, **args)


def _consume(store, entities, engine, **changes):
    args = dict(
        stock_id=entities.origin_stock_id, quantity=5.0,
        sku=SKU, reservation_reference="reservation:immutable",
        consumption_reference="consumption:immutable",
    )
    args.update(changes)
    return wm.consume_external_reservation(store, engine, **args)


def _receipt(store, entities, engine, **changes):
    args = dict(
        stock_id=entities.origin_stock_id, quantity=4.0,
        sku=SKU, receipt_reference="receipt:immutable",
    )
    args.update(changes)
    return wm.receive_external_replenishment(store, engine, **args)


def _assert_one_stock_event(
    store, entities, *, name, reference_field, reference, quantity,
):
    """Exactly one immutable business event must exist before any replay."""
    events = tuple(
        event for event in store.events()
        if event.name == name
    )
    assert len(events) == 1, f"expected one {name} publication, got {len(events)}"
    event = events[0]
    assert event.entity_type == "warehouse_management_stock"
    assert event.entity_id == entities.origin_stock_id
    assert event.payload[reference_field] == reference
    assert event.payload["sku"] == SKU
    assert event.payload["quantity"] == quantity
    return event


@pytest.mark.parametrize(
    "bad,match",
    [
        ({"sku": ""}, "sku cannot be empty"),
        ({"origin_on_hand": -1}, "origin_on_hand cannot be negative"),
        ({"dock_count": 0}, "dock_count must be at least one"),
        ({"forklift_count": 0}, "forklift_count must be at least one"),
        ({"transfer_quantity": 0}, "transfer_quantity must be positive"),
        ({"planned_completion_hours": 0}, "planned_completion_hours must be positive"),
        ({"instance_key": " "}, "instance_key must be a nonempty string"),
    ],
)
def test_warehouse_cannot_seed_invalid_physical_inventory(bad, match):
    store = MemoryPersistence()
    with pytest.raises(ValueError, match=match):
        wm.seed_reference(store, owner_id=OWNER, **bad)
    assert store.entities() == ()


@pytest.mark.parametrize(
    "change,match",
    [
        ({"quantity": 0}, "replenishment quantity must be positive"),
        ({"quantity": -1}, "replenishment quantity must be positive"),
        ({"sku": ""}, "replenishment sku cannot be empty"),
        ({"receipt_reference": ""}, "receipt_reference cannot be empty"),
        ({"sku": "alien-sku"}, "replenishment SKU mismatch"),
    ],
)
def test_replenishment_rejects_invalid_receipt_without_mutating_stock(change, match):
    store, entities, engine = _world()
    stock_before = _stock(store, entities)
    events_before = tuple(store.events())
    with pytest.raises(ValueError, match=match):
        _receipt(store, entities, engine, **change)
    assert _stock(store, entities) == stock_before
    assert tuple(store.events()) == events_before


def test_replenishment_replay_is_exactly_once_and_conflicting_quantity_is_rejected():
    store, entities, engine = _world()
    assert _receipt(store, entities, engine) is True
    assert _stock(store, entities).attributes["on_hand"] == 24.0
    first_event = _assert_one_stock_event(
        store, entities,
        name="warehouse_management.replenishment_received",
        reference_field="receipt_reference",
        reference="receipt:immutable", quantity=4.0,
    )
    events_before = tuple(store.events())
    assert first_event in events_before
    assert _receipt(store, entities, engine) is False
    assert tuple(store.events()) == events_before
    with pytest.raises(ValueError, match="receipt replay conflict"):
        _receipt(store, entities, engine, quantity=4.5)
    assert _stock(store, entities).attributes["on_hand"] == 24.0
    assert tuple(store.events()) == events_before


@pytest.mark.parametrize(
    "change,match",
    [
        ({"quantity": 0}, "reservation quantity must be positive"),
        ({"quantity": -1}, "reservation quantity must be positive"),
        ({"quantity": 0.0000001}, "below supported precision"),
        ({"sku": ""}, "reservation sku cannot be empty"),
        ({"reservation_reference": ""}, "reservation_reference cannot be empty"),
        ({"sku": "alien-sku"}, "reservation SKU mismatch"),
    ],
)
def test_reservation_rejects_invalid_claim_without_phantom_capacity(change, match):
    store, entities, engine = _world()
    before = _stock(store, entities)
    events_before = tuple(store.events())
    with pytest.raises(ValueError, match=match):
        _reserve(store, entities, engine, **change)
    assert _stock(store, entities) == before
    assert tuple(store.events()) == events_before


def test_finite_stock_rejects_overbooking_without_creating_a_reservation():
    store, entities, engine = _world(available=5.0)
    assert _reserve(store, entities, engine) is True
    before = _stock(store, entities)
    events_before = tuple(store.events())
    assert _reserve(
        store, entities, engine,
        quantity=1.0, reservation_reference="reservation:competitor",
    ) is False
    assert _stock(store, entities) == before
    assert tuple(store.events()) == events_before


def test_durable_reservation_is_idempotent_and_cannot_rebind_quantity_or_sku():
    store, entities, engine = _world()
    assert _reserve(store, entities, engine) is True
    record = _stock(store, entities).attributes["external_reservations"]["reservation:immutable"]
    assert record == {
        "sku": SKU,
        "quantity": 5.0,
        "consumed": False,
        "consumption_reference": None,
    }
    first_event = _assert_one_stock_event(
        store, entities,
        name="warehouse_management.inventory_reserved",
        reference_field="reservation_reference",
        reference="reservation:immutable", quantity=5.0,
    )
    events_before = tuple(store.events())
    assert first_event in events_before
    assert _reserve(store, entities, engine) is False
    assert tuple(store.events()) == events_before
    with pytest.raises(ValueError, match="reservation replay conflict"):
        _reserve(store, entities, engine, quantity=4.0)
    assert _stock(store, entities).attributes["reserved"] == 5.0
    assert tuple(store.events()) == events_before


@pytest.mark.parametrize(
    "change,match",
    [
        ({"quantity": 0}, "consumption quantity must be positive"),
        ({"quantity": -1}, "consumption quantity must be positive"),
        ({"quantity": 0.0000001}, "below supported precision"),
        ({"sku": ""}, "consumption sku cannot be empty"),
        ({"reservation_reference": ""}, "reservation_reference cannot be empty"),
        ({"consumption_reference": ""}, "consumption_reference cannot be empty"),
        ({"sku": "alien-sku"}, "consumption SKU mismatch"),
        ({"reservation_reference": "reservation:foreign"}, "unknown external reservation"),
        ({"quantity": 4}, "reservation consumption conflict"),
    ],
)
def test_consumption_rejects_unowned_or_inconsistent_reservation(change, match):
    store, entities, engine = _world()
    assert _reserve(store, entities, engine) is True
    before = _stock(store, entities)
    events_before = tuple(store.events())
    with pytest.raises(ValueError, match=match):
        _consume(store, entities, engine, **change)
    assert _stock(store, entities) == before
    assert tuple(store.events()) == events_before


def test_consumption_is_exactly_once_and_refuses_conflicting_receipt_identity():
    store, entities, engine = _world()
    assert _reserve(store, entities, engine) is True
    assert _consume(store, entities, engine) is True
    stock = _stock(store, entities)
    assert stock.attributes["on_hand"] == 15.0
    assert stock.attributes["reserved"] == 0.0
    record = stock.attributes["external_reservations"]["reservation:immutable"]
    assert record["consumed"] is True
    assert record["consumption_reference"] == "consumption:immutable"
    first_event = _assert_one_stock_event(
        store, entities,
        name="warehouse_management.inventory_consumed",
        reference_field="consumption_reference",
        reference="consumption:immutable", quantity=5.0,
    )
    assert first_event.payload["reservation_reference"] == "reservation:immutable"
    events_before = tuple(store.events())
    assert first_event in events_before
    assert _consume(store, entities, engine) is False
    with pytest.raises(ValueError, match="consumption replay conflict"):
        _consume(store, entities, engine, consumption_reference="consumption:forged")
    assert _stock(store, entities) == stock
    assert tuple(store.events()) == events_before


@pytest.mark.parametrize("field", ("on_hand", "reserved"))
def test_consumption_refuses_corrupted_projection_without_business_event(field):
    store, entities, engine = _world()
    assert _reserve(store, entities, engine) is True
    with store.transaction() as uow:
        stock = uow.get_entity("warehouse_management_stock", entities.origin_stock_id)
        stock.attributes[field] = 0.0
        uow.save_entity(stock)
    before = _stock(store, entities)
    events_before = tuple(store.events())
    with pytest.raises(RuntimeError, match="cannot satisfy consumption"):
        _consume(store, entities, engine)
    assert _stock(store, entities) == before
    assert tuple(store.events()) == events_before


def test_transfer_refuses_to_depart_when_stock_is_not_available():
    store, entities, engine = _world(available=2.0, transfer=8.0)
    assert wm.start_transfer(store, engine, entities=entities) is False
    shipment = store.entity("warehouse_management_shipment", entities.shipment_id)
    truck = store.entity("warehouse_management_truck", entities.truck_id)
    assert shipment.state == "planned"
    assert truck.state == "scheduled"
    assert _stock(store, entities).attributes["reserved"] == 0.0


def test_genuine_transfer_moves_stock_once_and_releases_exact_resources():
    store, entities, engine = _world()
    assert wm.complete_unload(store, engine, entities=entities) is False
    assert wm._release_completed_resources(store, engine, entities=entities) is False

    assert wm.start_transfer(store, engine, entities=entities) is True
    shipment = store.entity("warehouse_management_shipment", entities.shipment_id)
    assert shipment.state == "in_transit"
    assert shipment.attributes["stock_reserved"] is True
    assert shipment.attributes["departed_at"] is not None
    assert _stock(store, entities).attributes["reserved"] == 8.0

    assert wm.arrive_truck(store, engine, entities=entities) is True
    assert wm.assign_forklift(store, engine, entities=entities) is True
    shipment = store.entity("warehouse_management_shipment", entities.shipment_id)
    assert shipment.state == "handling"
    assert shipment.attributes["assigned_dock_id"] == entities.dock_ids[0]
    assert shipment.attributes["assigned_forklift_id"] == entities.forklift_ids[0]

    assert wm.complete_unload(store, engine, entities=entities) is True
    shipment = store.entity("warehouse_management_shipment", entities.shipment_id)
    assert shipment.state == "completed"
    assert shipment.attributes["stock_moved"] is True
    assert shipment.attributes["resources_released"] is True
    assert _stock(store, entities).attributes["on_hand"] == 12.0
    assert _stock(store, entities).attributes["reserved"] == 0.0
    dest = store.entity("warehouse_management_stock", entities.destination_stock_id)
    assert dest.attributes["on_hand"] == 8.0
    assert store.entity("warehouse_management_dock", entities.dock_ids[0]).state == "available"
    assert store.entity("warehouse_management_forklift", entities.forklift_ids[0]).state == "available"
    assert store.entity("warehouse_management_truck", entities.truck_id).state == "released"

    events_before = tuple(store.events())
    assert wm.complete_unload(store, engine, entities=entities) is True
    assert tuple(store.events()) == events_before
    assert _stock(store, entities).attributes["on_hand"] == 12.0
    assert store.entity("warehouse_management_stock", entities.destination_stock_id).attributes["on_hand"] == 8.0


@pytest.mark.parametrize("field", ("on_hand", "reserved"))
def test_handling_refuses_to_move_stock_against_corrupt_transfer_projection(field):
    store, entities, engine = _world()
    assert wm.start_transfer(store, engine, entities=entities) is True
    assert wm.arrive_truck(store, engine, entities=entities) is True
    assert wm.assign_forklift(store, engine, entities=entities) is True

    with store.transaction() as uow:
        stock = uow.get_entity("warehouse_management_stock", entities.origin_stock_id)
        stock.attributes[field] = 0.0
        uow.save_entity(stock)

    before = _stock(store, entities)
    destination_before = store.entity("warehouse_management_stock", entities.destination_stock_id)
    events_before = tuple(store.events())
    with pytest.raises(RuntimeError, match="reserved transfer stock is inconsistent"):
        wm.complete_unload(store, engine, entities=entities)
    assert _stock(store, entities) == before
    assert store.entity("warehouse_management_stock", entities.destination_stock_id) == destination_before
    assert tuple(store.events()) == events_before


def test_transfer_lifecycle_kpis_are_derived_from_durable_actual_times():
    store, entities, engine = _world()
    initial = wm.shipment_kpis(store, entities=entities)
    assert initial["completed"] is False
    assert initial["lead_time_seconds"] is None
    assert initial["lateness_seconds"] is None
    assert wm.start_transfer(store, engine, entities=entities) is True
    # Arrival occurs after 2h, and unloading finishes at 5h: the
    # planned deadline is ORIGIN+4h. Constant-zero and premature-departure
    # KPI implementations must fail both independent numeric assertions.
    engine.context.clock.advance(2)
    assert wm.arrive_truck(store, engine, entities=entities) is True
    assert wm.assign_forklift(store, engine, entities=entities) is True
    engine.context.clock.advance(3)
    assert wm.complete_unload(store, engine, entities=entities) is True
    final = wm.shipment_kpis(store, entities=entities)
    assert final["completed"] is True
    assert final["lead_time_seconds"] == 5 * 3600.0
    assert final["lateness_seconds"] == 1 * 3600.0
    assert final["on_time"] is False

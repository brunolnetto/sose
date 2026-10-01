from __future__ import annotations

import pytest

from sose.examples.warehouse_fulfillment.simulation import (
    _entity,
    allocate_order,
    allocation_id,
    build_runtime,
    correct_lot_balance,
    pack_order,
    pick_allocation,
    pick_order,
    seed_reference,
    ship_order,
)
from sose.persistence.memory import MemoryPersistence


def _runtime(**seed_kwargs):
    persistence = MemoryPersistence()
    entities = seed_reference(persistence, **seed_kwargs)
    _, engine = build_runtime(persistence)
    return persistence, entities, engine


def _save(persistence, entity):
    with persistence.transaction() as uow:
        uow.save_entity(entity)


def test_missing_entity_guard_is_observable():
    persistence = MemoryPersistence()

    with pytest.raises(RuntimeError, match="was not persisted"):
        _entity(persistence, "warehouse_fulfillment_order", "missing")


def test_allocate_order_terminal_and_non_requested_states_are_idempotent():
    persistence, entities, engine = _runtime()
    order = persistence.entity("warehouse_fulfillment_order", entities.order_id)
    assert order is not None

    order.state = "packed"
    _save(persistence, order)
    assert allocate_order(persistence, engine, entities=entities) is True

    order = persistence.entity("warehouse_fulfillment_order", entities.order_id)
    assert order is not None
    order.state = "cancelled"
    _save(persistence, order)
    assert allocate_order(persistence, engine, entities=entities) is False


def test_allocate_order_replays_existing_durable_allocation_index():
    persistence, entities, engine = _runtime()
    assert allocate_order(persistence, engine, entities=entities)

    order = persistence.entity("warehouse_fulfillment_order", entities.order_id)
    assert order is not None
    existing = tuple(order.attributes["allocation_ids"])
    assert existing

    # Recreate the interrupted boundary where allocations committed but the
    # order state transition was not observed.
    order.state = "requested"
    _save(persistence, order)

    assert allocate_order(persistence, engine, entities=entities) is True
    replayed = persistence.entity("warehouse_fulfillment_order", entities.order_id)
    assert replayed is not None and replayed.state == "allocated"
    assert tuple(replayed.attributes["allocation_ids"]) == existing


def test_allocate_order_rejects_missing_allocation_from_durable_index():
    persistence, entities, engine = _runtime()
    order = persistence.entity("warehouse_fulfillment_order", entities.order_id)
    assert order is not None
    order.attributes["allocation_ids"] = ["missing-allocation"]
    _save(persistence, order)

    with pytest.raises(RuntimeError, match="missing durable allocation"):
        allocate_order(persistence, engine, entities=entities)


def test_pick_allocation_is_idempotent_after_pick():
    persistence, entities, engine = _runtime()
    assert allocate_order(persistence, engine, entities=entities)
    order = persistence.entity("warehouse_fulfillment_order", entities.order_id)
    assert order is not None
    aid = str(order.attributes["allocation_ids"][0])

    first = pick_allocation(
        persistence,
        engine,
        entities=entities,
        allocation_id_value=aid,
    )
    second = pick_allocation(
        persistence,
        engine,
        entities=entities,
        allocation_id_value=aid,
    )

    assert first.state == second.state == "picked"


def test_pick_allocation_detects_corrupt_allocated_projection():
    persistence, entities, engine = _runtime()
    assert allocate_order(persistence, engine, entities=entities)
    order = persistence.entity("warehouse_fulfillment_order", entities.order_id)
    assert order is not None
    aid = str(order.attributes["allocation_ids"][0])
    allocation = persistence.entity("warehouse_allocation", aid)
    assert allocation is not None

    lot = persistence.entity("warehouse_inventory_lot", allocation.attributes["lot_id"])
    assert lot is not None
    lot.attributes["allocated"] = float(allocation.attributes["quantity"]) - 1.0
    _save(persistence, lot)

    with pytest.raises(RuntimeError, match="allocation projection"):
        pick_allocation(
            persistence,
            engine,
            entities=entities,
            allocation_id_value=aid,
        )


def test_pick_allocation_detects_corrupt_on_hand_projection():
    persistence, entities, engine = _runtime()
    assert allocate_order(persistence, engine, entities=entities)
    order = persistence.entity("warehouse_fulfillment_order", entities.order_id)
    assert order is not None
    aid = str(order.attributes["allocation_ids"][0])
    allocation = persistence.entity("warehouse_allocation", aid)
    assert allocation is not None

    lot = persistence.entity("warehouse_inventory_lot", allocation.attributes["lot_id"])
    assert lot is not None
    lot.attributes["on_hand"] = float(allocation.attributes["quantity"]) - 1.0
    _save(persistence, lot)

    with pytest.raises(RuntimeError, match="on-hand projection"):
        pick_allocation(
            persistence,
            engine,
            entities=entities,
            allocation_id_value=aid,
        )


def test_pick_order_rejects_unallocated_order_and_accepts_shipped_replay():
    persistence, entities, engine = _runtime()

    with pytest.raises(RuntimeError, match="pick requires allocated"):
        pick_order(persistence, engine, entities=entities)

    order = persistence.entity("warehouse_fulfillment_order", entities.order_id)
    assert order is not None
    order.state = "shipped"
    _save(persistence, order)

    assert pick_order(persistence, engine, entities=entities) is True


def test_pack_order_rejects_wrong_state_and_accepts_packed_replay():
    persistence, entities, engine = _runtime()

    with pytest.raises(RuntimeError, match="pack requires picking"):
        pack_order(persistence, engine, entities=entities)

    order = persistence.entity("warehouse_fulfillment_order", entities.order_id)
    assert order is not None
    order.state = "packed"
    _save(persistence, order)

    assert pack_order(persistence, engine, entities=entities) is True


def test_ship_order_rejects_wrong_state_and_accepts_shipped_replay():
    persistence, entities, engine = _runtime()

    with pytest.raises(RuntimeError, match="ship requires packed"):
        ship_order(persistence, engine, entities=entities)

    order = persistence.entity("warehouse_fulfillment_order", entities.order_id)
    assert order is not None
    order.state = "shipped"
    _save(persistence, order)

    assert ship_order(persistence, engine, entities=entities) is True


def test_correction_rejects_non_positive_sequence():
    persistence, entities, engine = _runtime()

    with pytest.raises(ValueError, match="sequence must be positive"):
        correct_lot_balance(
            persistence,
            engine,
            lot_id=entities.lot_ids[0],
            sequence=0,
            delta=1.0,
        )


def test_correction_negative_projection_guard_is_fault_injectable():
    persistence, entities, engine = _runtime()
    lot = persistence.entity("warehouse_inventory_lot", entities.lot_ids[0])
    assert lot is not None

    # A negative allocated projection is unsupported corrupted state. Injecting
    # it deliberately isolates the second balance guard: the resulting on-hand
    # is negative without also being below the corrupted allocated projection.
    lot.attributes["allocated"] = -2.0
    _save(persistence, lot)

    with pytest.raises(ValueError, match="on-hand negative"):
        correct_lot_balance(
            persistence,
            engine,
            lot_id=lot.id,
            sequence=1,
            delta=-7.0,
        )


def test_primary_only_allocation_does_not_use_substitute_when_short():
    persistence, entities, engine = _runtime(
        requested_quantity=7.0,
        primary_on_hand=6.0,
        substitute_on_hand=100.0,
        allow_substitute=False,
    )

    assert allocate_order(persistence, engine, entities=entities) is False
    order = persistence.entity("warehouse_fulfillment_order", entities.order_id)
    assert order is not None and order.attributes["allocation_ids"] == []


def test_allocation_identity_is_stable_for_seeded_lot():
    persistence, entities, engine = _runtime()
    assert allocate_order(persistence, engine, entities=entities)

    expected = allocation_id(entities.order_id, entities.lot_ids[0])
    allocation = persistence.entity("warehouse_allocation", expected)
    assert allocation is not None

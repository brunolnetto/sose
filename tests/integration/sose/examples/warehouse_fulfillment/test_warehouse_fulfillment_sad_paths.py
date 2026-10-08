import pytest

from sose.backends.simpy import SimPyBackend
from sose.examples.warehouse_fulfillment.scenarios import ORIGIN
from sose.examples.warehouse_fulfillment.simulation import (
    allocate_order,
    build_runtime,
    correct_lot_balance,
    occurrence_id,
    pack_order,
    seed_reference,
)
from sose.persistence.memory import MemoryPersistence


def _runtime():
    persistence = MemoryPersistence()
    entities = seed_reference(persistence)
    _, engine = build_runtime(persistence)
    backend = SimPyBackend(origin=ORIGIN)
    engine.rebuild_backend(backend)
    return persistence, entities, engine, backend


def test_insufficient_inventory_does_not_commit_partial_allocation():
    persistence, entities, engine, backend = _runtime()
    for lot_id in entities.lot_ids:
        lot = persistence.entity("warehouse_inventory_lot", lot_id)
        assert lot is not None
        lot.attributes["on_hand"] = 2.0
        with persistence.transaction() as uow:
            uow.save_entity(lot)

    assert allocate_order(persistence, engine, entities=entities) is False
    order = persistence.entity("warehouse_fulfillment_order", entities.order_id)
    assert order is not None and order.state == "requested"
    assert order.attributes["allocation_ids"] == []
    for lot_id in entities.lot_ids:
        lot = persistence.entity("warehouse_inventory_lot", lot_id)
        assert lot is not None and lot.attributes["allocated"] == 0.0


def test_pack_requires_every_committed_allocation_to_be_picked():
    persistence, entities, engine, backend = _runtime()
    assert allocate_order(persistence, engine, entities=entities)
    order = persistence.entity("warehouse_fulfillment_order", entities.order_id)
    assert order is not None
    assert pick_order(
        persistence,
        engine,
        backend,
        entities=entities,
    ) is False
    first_due = min(work.due_at for work in persistence.scheduled_work())
    backend.run_until(first_due)
    assert pick_order(
        persistence,
        engine,
        backend,
        entities=entities,
    ) is False

    with pytest.raises(RuntimeError, match="all allocations"):
        pack_order(persistence, engine, backend, entities=entities)


def test_inventory_correction_changes_projection_without_rewriting_evidence():
    persistence, entities, engine, backend = _runtime()
    lot_id = entities.lot_ids[0]

    first = correct_lot_balance(
        persistence,
        engine,
        lot_id=lot_id,
        sequence=1,
        delta=2.0,
    )
    repeated = correct_lot_balance(
        persistence,
        engine,
        lot_id=lot_id,
        sequence=1,
        delta=2.0,
    )
    lot = persistence.entity("warehouse_inventory_lot", lot_id)

    assert first.id == repeated.id
    assert first.state == "committed"
    assert lot is not None and lot.attributes["on_hand"] == 8.0
    assert lot.attributes["occurrence_ids"] == [first.id]

    with pytest.raises(ValueError, match="different evidence"):
        correct_lot_balance(
            persistence,
            engine,
            lot_id=lot_id,
            sequence=1,
            delta=-1.0,
        )

    persisted = persistence.entity(
        "warehouse_inventory_occurrence",
        occurrence_id("correction", lot_id, 1),
    )
    assert persisted is not None
    assert persisted.attributes["delta_on_hand"] == 2.0
    assert persisted.attributes["resulting_on_hand"] == 8.0


def test_correction_cannot_erase_committed_allocation_capacity():
    persistence, entities, engine, backend = _runtime()
    assert allocate_order(persistence, engine, entities=entities)

    with pytest.raises(ValueError, match="below allocated"):
        correct_lot_balance(
            persistence,
            engine,
            lot_id=entities.lot_ids[0],
            sequence=1,
            delta=-1.0,
        )

from __future__ import annotations

from sose.backends.simpy import SimPyBackend
from sose.examples.logistics.simulation import (
    ORIGIN,
    PICKUP_DUE,
    build_runtime,
    delivery_attempt_id,
    reconcile_delivery_dispatch,
    reconcile_origin_hub,
    reconcile_pickup,
    reconcile_transfer,
    seed_reference,
)
from sose.persistence.memory import MemoryPersistence


ORGANIZATION_ID = "logistics-backpressure-organization"


def _runtime(*, queue_capacity: int = 1):
    persistence = MemoryPersistence()
    entities = seed_reference(
        persistence,
        owner_id=ORGANIZATION_ID,
        hub_queue_capacity=queue_capacity,
    )
    _, engine = build_runtime(persistence, owner_id=ORGANIZATION_ID)
    backend = SimPyBackend(origin=ORIGIN)
    engine.rebuild_backend(backend)
    backend.run_until(PICKUP_DUE)
    return persistence, entities, engine, backend


def _drain(engine, backend, *, store_name: str, request_id: str):
    result = engine.stores.ensure_selection(
        backend,
        store_name=store_name,
        request_id=request_id,
        requested_at=backend.now,
    )
    assert result is not None
    backend.run_until(backend.now)
    return result


def test_origin_hub_queue_backpressure_recovers_without_duplicate_arrival():
    persistence, entities, engine, backend = _runtime()
    assert reconcile_pickup(persistence, engine, backend, entities=entities)

    engine.stores.put(
        backend,
        store_name=f"{ORGANIZATION_ID}/origin_hub_queue",
        item_id="origin-blocker",
        value={"shipment_id": "blocker"},
        requested_at=backend.now,
    )
    backend.run_until(backend.now)

    assert reconcile_origin_hub(
        persistence,
        engine,
        backend,
        entities=entities,
    ) is False
    shipment = persistence.entity("shipment", entities.shipment_id)
    assert shipment is not None and shipment.state == "picked_up"

    request_id = f"origin-dock:{entities.shipment_id}"
    assert engine.resources.reservation_for(request_id) is not None
    pending_origin = [
        intent
        for intent in persistence.store_put_intents()
        if intent.item_id == f"origin-queue:{entities.shipment_id}"
    ]
    assert len(pending_origin) == 1
    assert pending_origin[0].store_name == f"{ORGANIZATION_ID}/origin_hub_queue"

    _drain(
        engine,
        backend,
        store_name=f"{ORGANIZATION_ID}/origin_hub_queue",
        request_id="drain-origin-blocker",
    )

    assert reconcile_origin_hub(
        persistence,
        engine,
        backend,
        entities=entities,
    ) is True
    shipment = persistence.entity("shipment", entities.shipment_id)
    assert shipment is not None and shipment.state == "at_origin_hub"
    assert engine.resources.has_request(request_id) is False

    events_before = tuple(persistence.events())
    assert reconcile_origin_hub(
        persistence,
        engine,
        backend,
        entities=entities,
    ) is True
    assert tuple(persistence.events()) == events_before


def test_destination_queue_backpressure_recovers_after_durable_origin_dequeue():
    persistence, entities, engine, backend = _runtime()
    assert reconcile_pickup(persistence, engine, backend, entities=entities)
    assert reconcile_origin_hub(persistence, engine, backend, entities=entities)

    engine.stores.put(
        backend,
        store_name=f"{ORGANIZATION_ID}/destination_hub_queue",
        item_id="destination-blocker",
        value={"shipment_id": "blocker"},
        requested_at=backend.now,
    )
    backend.run_until(backend.now)

    assert reconcile_transfer(
        persistence,
        engine,
        backend,
        entities=entities,
    ) is False
    shipment = persistence.entity("shipment", entities.shipment_id)
    assert shipment is not None and shipment.state == "in_transfer"

    assert any(
        result.request_id == f"origin-dequeue:{entities.shipment_id}"
        for result in persistence.store_get_results()
    )
    transfer_request = f"transfer-vehicle:{entities.shipment_id}"
    dock_request = f"destination-dock:{entities.shipment_id}"
    assert engine.resources.reservation_for(transfer_request) is not None
    assert engine.resources.reservation_for(dock_request) is not None
    pending_destination = [
        intent
        for intent in persistence.store_put_intents()
        if intent.item_id == f"destination-queue:{entities.shipment_id}"
    ]
    assert len(pending_destination) == 1
    assert pending_destination[0].store_name == f"{ORGANIZATION_ID}/destination_hub_queue"

    _drain(
        engine,
        backend,
        store_name=f"{ORGANIZATION_ID}/destination_hub_queue",
        request_id="drain-destination-blocker",
    )

    assert reconcile_transfer(
        persistence,
        engine,
        backend,
        entities=entities,
    ) is True
    shipment = persistence.entity("shipment", entities.shipment_id)
    assert shipment is not None and shipment.state == "at_destination_hub"
    assert engine.resources.has_request(transfer_request) is False
    assert engine.resources.has_request(dock_request) is False


def test_transfer_replay_after_destination_arrival_has_no_new_side_effects():
    persistence, entities, engine, backend = _runtime(queue_capacity=2)
    assert reconcile_pickup(persistence, engine, backend, entities=entities)
    assert reconcile_origin_hub(persistence, engine, backend, entities=entities)
    assert reconcile_transfer(persistence, engine, backend, entities=entities)

    before = (
        tuple(persistence.events()),
        tuple(persistence.store_get_results()),
        tuple(persistence.store_put_intents()),
        tuple(persistence.resource_reservations()),
    )

    assert reconcile_transfer(persistence, engine, backend, entities=entities)

    after = (
        tuple(persistence.events()),
        tuple(persistence.store_get_results()),
        tuple(persistence.store_put_intents()),
        tuple(persistence.resource_reservations()),
    )
    assert after == before


def test_delivery_dispatch_replay_keeps_single_attempt_and_selection():
    persistence, entities, engine, backend = _runtime(queue_capacity=2)
    assert reconcile_pickup(persistence, engine, backend, entities=entities)
    assert reconcile_origin_hub(persistence, engine, backend, entities=entities)
    assert reconcile_transfer(persistence, engine, backend, entities=entities)
    assert reconcile_delivery_dispatch(
        persistence,
        engine,
        backend,
        entities=entities,
        ordinal=1,
    )

    attempt = persistence.entity("delivery_attempt", delivery_attempt_id(1))
    assert attempt is not None and attempt.state == "out_for_delivery"
    before = (
        tuple(persistence.events()),
        tuple(persistence.store_get_results()),
        tuple(persistence.store_put_intents()),
        tuple(persistence.resource_reservations()),
    )

    assert reconcile_delivery_dispatch(
        persistence,
        engine,
        backend,
        entities=entities,
        ordinal=1,
    )

    after = (
        tuple(persistence.events()),
        tuple(persistence.store_get_results()),
        tuple(persistence.store_put_intents()),
        tuple(persistence.resource_reservations()),
    )
    assert after == before


def test_destination_put_consumed_immediately_still_completes_transfer(monkeypatch):
    """A durable queue GET receipt is authoritative even after the item disappears."""
    persistence, entities, engine, backend = _runtime(queue_capacity=2)
    assert reconcile_pickup(persistence, engine, backend, entities=entities)
    assert reconcile_origin_hub(persistence, engine, backend, entities=entities)

    original = engine.stores.ensure_put
    received = []

    def immediately_deliver_to_consumer(*args, **kwargs):
        item = original(*args, **kwargs)
        if kwargs["store_name"] == f"{ORGANIZATION_ID}/destination_hub_queue" and item is not None:
            receipt = engine.stores.ensure_selection(
                backend,
                store_name=f"{ORGANIZATION_ID}/destination_hub_queue",
                request_id="destination-receiver:one",
                requested_at=backend.now,
            )
            assert receipt is not None
            received.append(receipt)
        return item

    monkeypatch.setattr(engine.stores, "ensure_put", immediately_deliver_to_consumer)
    assert reconcile_transfer(persistence, engine, backend, entities=entities) is True

    shipment = persistence.entity("shipment", entities.shipment_id)
    assert shipment is not None and shipment.state == "at_destination_hub"
    item_id = f"destination-queue:{entities.shipment_id}"
    assert len(received) == 1
    assert received[0].item.item_id == item_id
    assert not any(item.item_id == item_id for item in persistence.store_items())
    assert not any(intent.item_id == item_id for intent in persistence.store_put_intents())

    before = (tuple(persistence.events()), tuple(persistence.store_get_results()))
    assert reconcile_transfer(persistence, engine, backend, entities=entities) is True
    assert (tuple(persistence.events()), tuple(persistence.store_get_results())) == before

from __future__ import annotations

import pytest

from sose.backends.simpy import SimPyBackend
from sose.examples.logistics.simulation import (
    LogisticsEntities,
    ORIGIN,
    PICKUP_DUE,
    build_runtime,
    delivery_attempt_id,
    ensure_delivery_attempt,
    reconcile_delivery_dispatch,
    reconcile_delivery_failure,
    reconcile_delivery_success,
    reconcile_origin_hub,
    reconcile_pickup,
    reconcile_transfer,
    seed_reference,
)
from sose.persistence.memory import MemoryPersistence


def _runtime(*, queue_capacity=10):
    persistence = MemoryPersistence()
    entities = seed_reference(
        persistence,
        hub_queue_capacity=queue_capacity,
    )
    _, engine = build_runtime(persistence)
    backend = SimPyBackend(origin=ORIGIN)
    engine.rebuild_backend(backend)
    backend.run_until(PICKUP_DUE)
    return persistence, entities, engine, backend


def _save(persistence, entity):
    with persistence.transaction() as uow:
        uow.save_entity(entity)


def _occupy(engine, backend, resource_name, request_id):
    reservation = engine.resources.ensure_requested(
        backend,
        resource_name=resource_name,
        request_id=request_id,
        requested_at=backend.now,
    )
    assert reservation is not None
    return reservation


def _to_origin_hub(persistence, entities, engine, backend):
    assert reconcile_pickup(persistence, engine, backend, entities=entities)
    assert reconcile_origin_hub(persistence, engine, backend, entities=entities)


def _to_destination_hub(persistence, entities, engine, backend):
    _to_origin_hub(persistence, entities, engine, backend)
    assert reconcile_transfer(persistence, engine, backend, entities=entities)


def test_delivery_attempt_identity_rejects_non_positive_ordinal():
    with pytest.raises(ValueError, match="ordinal must be >= 1"):
        delivery_attempt_id(0)


def test_missing_shipment_guard_is_observable():
    persistence = MemoryPersistence()
    _, engine = build_runtime(persistence)
    backend = SimPyBackend(origin=ORIGIN)
    engine.rebuild_backend(backend)

    with pytest.raises(RuntimeError, match="shipment was not persisted"):
        reconcile_pickup(
            persistence,
            engine,
            backend,
            entities=LogisticsEntities(shipment_id="missing"),
        )


def test_pickup_is_idempotent_after_pickup_and_waits_under_resource_contention():
    persistence, entities, engine, backend = _runtime()
    blocker = _occupy(engine, backend, "pickup_courier", "pickup-blocker")

    assert reconcile_pickup(persistence, engine, backend, entities=entities) is False
    request_id = f"pickup-courier:{entities.shipment_id}"
    assert engine.resources.has_request(request_id)
    assert engine.resources.reservation_for(request_id) is None

    assert engine.resources.withdraw(backend, blocker.request_id)
    backend.run_until(backend.now)
    assert reconcile_pickup(persistence, engine, backend, entities=entities) is True
    assert persistence.entity("shipment", entities.shipment_id).state == "picked_up"

    events_before = tuple(persistence.events())
    assert reconcile_pickup(persistence, engine, backend, entities=entities) is True
    assert tuple(persistence.events()) == events_before


def test_origin_hub_rejects_wrong_state_and_is_idempotent_after_arrival():
    persistence, entities, engine, backend = _runtime()
    assert reconcile_origin_hub(
        persistence, engine, backend, entities=entities
    ) is False

    assert reconcile_pickup(persistence, engine, backend, entities=entities)
    assert reconcile_origin_hub(persistence, engine, backend, entities=entities)
    events_before = tuple(persistence.events())

    assert reconcile_origin_hub(persistence, engine, backend, entities=entities)
    assert tuple(persistence.events()) == events_before


def test_origin_hub_waits_for_dock_capacity():
    persistence, entities, engine, backend = _runtime()
    assert reconcile_pickup(persistence, engine, backend, entities=entities)
    blocker = _occupy(engine, backend, "origin_dock", "origin-dock-blocker")

    assert reconcile_origin_hub(
        persistence, engine, backend, entities=entities
    ) is False
    request_id = f"origin-dock:{entities.shipment_id}"
    assert engine.resources.has_request(request_id)
    assert engine.resources.reservation_for(request_id) is None

    assert engine.resources.withdraw(backend, blocker.request_id)


def test_origin_hub_waits_when_fifo_store_is_full():
    persistence, entities, engine, backend = _runtime(queue_capacity=1)
    assert reconcile_pickup(persistence, engine, backend, entities=entities)

    engine.stores.put(
        backend,
        store_name="origin_hub_queue",
        item_id="origin-blocker",
        value={"shipment_id": "blocker"},
        requested_at=backend.now,
    )
    backend.run_until(backend.now)

    assert reconcile_origin_hub(
        persistence, engine, backend, entities=entities
    ) is False
    shipment = persistence.entity("shipment", entities.shipment_id)
    assert shipment is not None and shipment.state == "picked_up"


def test_transfer_waits_for_vehicle_capacity():
    persistence, entities, engine, backend = _runtime()
    _to_origin_hub(persistence, entities, engine, backend)
    blocker = _occupy(engine, backend, "transfer_vehicle", "transfer-blocker")

    assert reconcile_transfer(
        persistence, engine, backend, entities=entities
    ) is False
    request_id = f"transfer-vehicle:{entities.shipment_id}"
    assert engine.resources.has_request(request_id)
    assert engine.resources.reservation_for(request_id) is None

    assert engine.resources.withdraw(backend, blocker.request_id)


def test_transfer_waits_for_destination_dock_after_durable_dequeue():
    persistence, entities, engine, backend = _runtime()
    _to_origin_hub(persistence, entities, engine, backend)
    blocker = _occupy(engine, backend, "destination_dock", "destination-dock-blocker")

    assert reconcile_transfer(
        persistence, engine, backend, entities=entities
    ) is False
    shipment = persistence.entity("shipment", entities.shipment_id)
    assert shipment is not None and shipment.state == "in_transfer"

    transfer_request = f"transfer-vehicle:{entities.shipment_id}"
    dock_request = f"destination-dock:{entities.shipment_id}"
    assert engine.resources.reservation_for(transfer_request) is not None
    assert engine.resources.has_request(dock_request)
    assert engine.resources.reservation_for(dock_request) is None

    assert engine.resources.withdraw(backend, blocker.request_id)


def test_transfer_is_idempotent_after_destination_arrival():
    persistence, entities, engine, backend = _runtime()
    _to_destination_hub(persistence, entities, engine, backend)
    events_before = tuple(persistence.events())

    assert reconcile_transfer(persistence, engine, backend, entities=entities)
    assert tuple(persistence.events()) == events_before


def test_delivery_attempt_creation_is_idempotent():
    persistence, _, engine, _ = _runtime()

    first = ensure_delivery_attempt(persistence, engine, ordinal=1)
    second = ensure_delivery_attempt(persistence, engine, ordinal=1)

    assert second == first
    assert sum(1 for entity in persistence.entities() if entity.entity_type == "delivery_attempt") == 1


def test_delivery_dispatch_rejects_wrong_shipment_state():
    persistence, entities, engine, backend = _runtime()

    assert reconcile_delivery_dispatch(
        persistence,
        engine,
        backend,
        entities=entities,
        ordinal=1,
    ) is False
    assert persistence.entity("delivery_attempt", delivery_attempt_id(1)) is None


def test_delivery_dispatch_respects_attempt_terminal_state():
    persistence, entities, engine, backend = _runtime()
    _to_destination_hub(persistence, entities, engine, backend)
    attempt = ensure_delivery_attempt(persistence, engine, ordinal=1)
    attempt.state = "failed"
    _save(persistence, attempt)

    assert reconcile_delivery_dispatch(
        persistence,
        engine,
        backend,
        entities=entities,
        ordinal=1,
    ) is False


def test_delivery_dispatch_is_idempotent_for_active_attempt():
    persistence, entities, engine, backend = _runtime()
    _to_destination_hub(persistence, entities, engine, backend)
    assert reconcile_delivery_dispatch(
        persistence,
        engine,
        backend,
        entities=entities,
        ordinal=1,
    )
    events_before = tuple(persistence.events())

    assert reconcile_delivery_dispatch(
        persistence,
        engine,
        backend,
        entities=entities,
        ordinal=1,
    )
    assert tuple(persistence.events()) == events_before


def test_delivery_dispatch_waits_for_courier_capacity():
    persistence, entities, engine, backend = _runtime()
    _to_destination_hub(persistence, entities, engine, backend)
    blocker = _occupy(engine, backend, "delivery_courier", "delivery-blocker")

    assert reconcile_delivery_dispatch(
        persistence,
        engine,
        backend,
        entities=entities,
        ordinal=1,
    ) is False

    attempt = persistence.entity("delivery_attempt", delivery_attempt_id(1))
    assert attempt is not None and attempt.state == "pending"
    request_id = f"delivery-courier:{attempt.id}"
    assert engine.resources.has_request(request_id)
    assert engine.resources.reservation_for(request_id) is None
    assert engine.resources.withdraw(backend, blocker.request_id)


def test_delivery_success_requires_active_attempt():
    persistence, entities, engine, backend = _runtime()

    with pytest.raises(RuntimeError, match="delivery attempt is not active"):
        reconcile_delivery_success(
            persistence,
            engine,
            backend,
            entities=entities,
            ordinal=1,
        )


def test_delivery_success_requires_out_for_delivery_shipment():
    persistence, entities, engine, backend = _runtime()
    attempt = ensure_delivery_attempt(persistence, engine, ordinal=1)
    attempt.state = "out_for_delivery"
    _save(persistence, attempt)

    with pytest.raises(RuntimeError, match="shipment is not out for delivery"):
        reconcile_delivery_success(
            persistence,
            engine,
            backend,
            entities=entities,
            ordinal=1,
        )


def test_delivery_failure_requires_active_attempt():
    persistence, entities, engine, backend = _runtime()

    with pytest.raises(RuntimeError, match="delivery attempt is not active"):
        reconcile_delivery_failure(
            persistence,
            engine,
            backend,
            entities=entities,
            ordinal=1,
        )


def test_delivery_failure_requires_out_for_delivery_shipment():
    persistence, entities, engine, backend = _runtime()
    attempt = ensure_delivery_attempt(persistence, engine, ordinal=1)
    attempt.state = "out_for_delivery"
    _save(persistence, attempt)

    with pytest.raises(RuntimeError, match="shipment is not out for delivery"):
        reconcile_delivery_failure(
            persistence,
            engine,
            backend,
            entities=entities,
            ordinal=1,
        )

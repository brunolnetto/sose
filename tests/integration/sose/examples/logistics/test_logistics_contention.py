from sose.backends.simpy import SimPyBackend
from sose.examples.logistics.simulation import (
    ORIGIN,
    PICKUP_DUE,
    build_runtime,
    reconcile_origin_hub,
    reconcile_pickup,
    reconcile_transfer,
    seed_reference,
)
from sose.persistence.memory import MemoryPersistence


ORGANIZATION_ID = "logistics-contention-organization"


def test_origin_hub_queue_survives_transfer_contention():
    persistence = MemoryPersistence()
    entities = seed_reference(persistence, owner_id=ORGANIZATION_ID)
    _, engine = build_runtime(persistence, owner_id=ORGANIZATION_ID)
    backend = SimPyBackend(origin=ORIGIN)
    engine.rebuild_backend(backend)

    backend.run_until(PICKUP_DUE)
    assert reconcile_pickup(persistence, engine, backend, entities=entities)
    assert reconcile_origin_hub(persistence, engine, backend, entities=entities)

    engine.resources.request(
        backend,
        resource_name="transfer_vehicle",
        request_id="transfer-blocker",
        requested_at=backend.now,
        priority=1,
    )
    backend.run_until(backend.now)

    assert reconcile_transfer(
        persistence, engine, backend, entities=entities
    ) is False
    assert persistence.entity("shipment", entities.shipment_id).state == "at_origin_hub"
    assert [item.store_name for item in persistence.store_items()] == [
        f"{ORGANIZATION_ID}/origin_hub_queue"
    ]
    assert any(
        demand.request_id == f"transfer-vehicle:{entities.shipment_id}"
        for demand in persistence.resource_demands()
    )

    blocker = next(
        reservation
        for reservation in persistence.resource_reservations()
        if reservation.request_id == "transfer-blocker"
    )
    engine.resources.release(backend, blocker.reservation_id)
    backend.run_until(backend.now)

    assert reconcile_transfer(
        persistence, engine, backend, entities=entities
    ) is True
    assert persistence.entity(
        "shipment", entities.shipment_id
    ).state == "at_destination_hub"
    assert [item.store_name for item in persistence.store_items()] == [
        f"{ORGANIZATION_ID}/destination_hub_queue"
    ]

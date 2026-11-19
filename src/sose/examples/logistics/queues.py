"""Owner-qualified hub queue dequeue/enqueue helpers for logistics (split from simulation.py)."""

from __future__ import annotations

from sose.backends.simpy import SimPyBackend
from sose.core.engine import Engine
from sose.persistence.memory import MemoryPersistence

from .records import _organization_queue


def _has_store_get_result(persistence: MemoryPersistence, request_id: str) -> bool:
    return any(
        result.request_id == request_id
        for result in persistence.store_get_results()
    )


def _has_store_get_request(persistence: MemoryPersistence, request_id: str) -> bool:
    return any(
        request.request_id == request_id
        for request in persistence.store_get_requests()
    )


def _ensure_queue_dequeue(
    persistence: MemoryPersistence, engine: Engine, backend: SimPyBackend,
    *, queue: str, prefix: str, shipment_id: str,
) -> bool:
    dequeue_id = f"{prefix}:{shipment_id}"
    if _has_store_get_result(persistence, dequeue_id):
        return True
    if not _has_store_get_request(persistence, dequeue_id):
        engine.stores.get(
            backend,
            store_name=_organization_queue(engine.owner_id, queue),
            request_id=dequeue_id,
            requested_at=backend.now,
        )
    backend.run_until(backend.now)
    return _has_store_get_result(persistence, dequeue_id)


def _ensure_origin_queue_dequeue(persistence, engine, backend, *, shipment_id: str) -> bool:
    return _ensure_queue_dequeue(
        persistence, engine, backend,
        queue="origin_hub_queue", prefix="origin-dequeue", shipment_id=shipment_id,
    )


def _ensure_destination_queue_dequeue(persistence, engine, backend, *, shipment_id: str) -> bool:
    return _ensure_queue_dequeue(
        persistence, engine, backend,
        queue="destination_hub_queue", prefix="destination-dequeue", shipment_id=shipment_id,
    )

def _destination_queued(persistence, engine, backend, shipment) -> bool:
    item_id = f"destination-queue:{shipment.id}"
    destination_queue = _organization_queue(
        engine.owner_id, "destination_hub_queue",
    )
    if not any(
        item.item_id == item_id and item.store_name == destination_queue
        for item in persistence.store_items()
    ):
        # Reuse the exact pending PUT after finite-queue backpressure.
        engine.stores.ensure_put(
            backend,
            store_name=destination_queue,
            item_id=item_id,
            value={"shipment_id": shipment.id},
            requested_at=backend.now,
        )
        backend.run_until(backend.now)

    # A concurrent consumer may have accepted and removed the item already.
    # Its durable receipt is equivalent to a live destination-queue item.
    destination_available = any(
        item.item_id == item_id and item.store_name == destination_queue
        for item in persistence.store_items()
    )
    destination_consumed = any(
        result.item.item_id == item_id
        and result.store_name == destination_queue
        for result in persistence.store_get_results()
    )
    return destination_available or destination_consumed

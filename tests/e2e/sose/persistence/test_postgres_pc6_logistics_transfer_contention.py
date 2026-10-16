"""Deterministic live-PostgreSQL Logistics transfer contention falsifiers (#489).

An independent session holds the exact finite vehicle, destination dock, or
destination queue while the shipment's own real StateChart attempts transfer.
Neither a random multiworker rerun nor a mocked resource failure qualifies.
"""
from __future__ import annotations

import os
from threading import Event, Thread
from uuid import uuid4

import pytest

from sose.backends.simpy import SimPyBackend
from sose.examples.logistics import simulation as logistics
from sose.persistence.postgres import PostgresPersistence

DSN = os.environ.get("SOSE_TEST_POSTGRES_DSN")
pytestmark = pytest.mark.skipif(not DSN, reason="SOSE_TEST_POSTGRES_DSN required")
SHIPMENT_OWNER = "pc6-transfer-shipment-organization"
HOLDER_OWNER = "pc6-transfer-holder-organization"


def _rebuild(store, *, owner_id):
    """Recreate ephemeral SimPy exactly at this owner's durable clock."""
    position = store.simulation_position(owner_id=owner_id)
    origin = logistics.ORIGIN if position is None else position.logical_time
    _, engine = logistics.build_runtime(store, owner_id=owner_id, now=origin)
    backend = SimPyBackend(origin=origin)
    engine.rebuild_backend(backend)
    return engine, backend


def _snapshot(store):
    """Complete comparable evidence for repeated transfer reconciliation."""
    return (
        tuple(store.events()),
        tuple(store.resource_demands()),
        tuple(store.resource_reservations()),
        tuple(store.resource_release_intents()),
        tuple(store.store_items()),
        tuple(store.store_put_intents()),
        tuple(store.store_get_requests()),
        tuple(store.store_get_results()),
    )


@pytest.mark.parametrize(
    ("blocker", "blocked_resource"),
    [
        ("transfer_vehicle", "transfer_vehicle"),
        ("destination_dock", "destination_dock"),
        ("destination_put", None),
    ],
)
def test_pg_transfer_finite_contention_retries_across_independent_sessions(
    blocker, blocked_resource,
):
    assert DSN is not None
    namespace = "pc6_transfer_finite_" + uuid4().hex[:12]
    held = Event()
    release = Event()
    released = Event()
    holder_errors = []
    holder_request = f"foreign-holder:{blocker}"

    # Bootstrap real business truth; the contender must be at the origin hub,
    # not a synthetic pre-granted reservation or an unowned fixture.
    with PostgresPersistence(DSN, namespace=namespace) as store:
        shipment = logistics.seed_reference(
            store, owner_id=SHIPMENT_OWNER, instance_key=namespace,
            resource_capacity=1, hub_queue_capacity=1,
        )
        engine, backend = _rebuild(store, owner_id=SHIPMENT_OWNER)
        backend.run_until(logistics.PICKUP_DUE)
        assert logistics.reconcile_pickup(store, engine, backend, entities=shipment)
        assert logistics.reconcile_origin_hub(store, engine, backend, entities=shipment)
        assert store.entity("shipment", shipment.shipment_id).state == "at_origin_hub"

    def hold_foreign_capacity():
        try:
            # A live second SQL connection owns the finite capacity until the
            # contender proves a durable wait. No transaction is held open
            # across Event.wait(): only committed physical truth is shared.
            with PostgresPersistence(DSN, namespace=namespace) as store:
                engine, backend = _rebuild(store, owner_id=HOLDER_OWNER)
                if blocked_resource is not None:
                    granted = engine.resources.ensure_requested(
                        backend, resource_name=blocked_resource,
                        request_id=holder_request, requested_at=backend.now,
                    )
                    assert granted is not None
                    assert engine.resources.reservation_for(holder_request) == granted
                else:
                    queued = engine.stores.ensure_put(
                        backend,
                        store_name=logistics._organization_queue(
                            SHIPMENT_OWNER, "destination_hub_queue",
                        ),
                        item_id=holder_request,
                        value={"shipment_id": "independent-holder"},
                        requested_at=backend.now,
                    )
                    backend.run_until(backend.now)
                    assert queued is not None
                held.set()
                if not release.wait(timeout=40):
                    raise TimeoutError("contender never reached blocked transfer")
                if blocked_resource is not None:
                    assert engine.resources.withdraw(backend, holder_request)
                else:
                    selection = engine.stores.ensure_selection(
                        backend,
                        store_name=logistics._organization_queue(
                            SHIPMENT_OWNER, "destination_hub_queue",
                        ),
                        request_id=f"foreign-dequeue:{blocker}",
                        requested_at=backend.now,
                    )
                    assert selection is not None
                    assert selection.item.item_id == holder_request
                    backend.run_until(backend.now)
        except BaseException as exc:
            holder_errors.append(exc)
            held.set()
        finally:
            released.set()

    thread = Thread(target=hold_foreign_capacity, daemon=True)
    thread.start()
    try:
        assert held.wait(timeout=30), "second PostgreSQL worker never held capacity"
        assert not holder_errors, f"foreign worker failed before contention: {holder_errors!r}"

        with PostgresPersistence(DSN, namespace=namespace) as store:
            engine, backend = _rebuild(store, owner_id=SHIPMENT_OWNER)
            assert logistics.reconcile_transfer(
                store, engine, backend, entities=shipment,
            ) is False
            business = store.entity("shipment", shipment.shipment_id)
            assert business is not None
            expected = (
                "at_origin_hub" if blocker == "transfer_vehicle" else "in_transfer"
            )
            assert business.state == expected
            transfer_request = f"transfer-vehicle:{shipment.shipment_id}"
            dock_request = f"destination-dock:{shipment.shipment_id}"
            destination_item = f"destination-queue:{shipment.shipment_id}"
            if blocker == "transfer_vehicle":
                assert engine.resources.has_request(transfer_request)
                assert engine.resources.reservation_for(transfer_request) is None
            elif blocker == "destination_dock":
                assert engine.resources.reservation_for(transfer_request) is not None
                assert engine.resources.has_request(dock_request)
                assert engine.resources.reservation_for(dock_request) is None
            else:
                assert engine.resources.reservation_for(dock_request) is not None
                assert sum(
                    item.item_id == destination_item
                    for item in store.store_put_intents()
                ) == 1
            # No second active physical reservation can be granted while the
            # foreign one is still committed. Queue PUTs retain one intent.
            if blocked_resource is not None:
                assert [
                    reservation.request_id
                    for reservation in store.resource_reservations()
                    if reservation.resource_name == blocked_resource
                ] == [holder_request]
            events = store.events()
            assert len(events) == len({event.event_id for event in events})
    finally:
        release.set()
        thread.join(timeout=35)

    assert not thread.is_alive(), "foreign worker blocked after release signal"
    assert not holder_errors, f"foreign worker failed: {holder_errors!r}"
    assert released.is_set()

    # Real connection death/reopen: the contender's old SimPy Environment is
    # discarded. Rehydrate durable demand/selection/PUT exactly once.
    with PostgresPersistence(DSN, namespace=namespace) as store:
        engine, backend = _rebuild(store, owner_id=SHIPMENT_OWNER)
        assert logistics.reconcile_transfer(
            store, engine, backend, entities=shipment,
        ) is True
        assert store.entity("shipment", shipment.shipment_id).state == "at_destination_hub"
        assert engine.resources.has_request(f"transfer-vehicle:{shipment.shipment_id}") is False
        assert engine.resources.has_request(f"destination-dock:{shipment.shipment_id}") is False
        assert not any(
            intent.item_id == f"destination-queue:{shipment.shipment_id}"
            for intent in store.store_put_intents()
        )
        assert not any(
            reservation.request_id == holder_request
            for reservation in store.resource_reservations()
        )
        canonical = _snapshot(store)
        assert len(store.events()) == len({event.event_id for event in store.events()})

    with PostgresPersistence(DSN, namespace=namespace) as store:
        engine, backend = _rebuild(store, owner_id=SHIPMENT_OWNER)
        assert logistics.reconcile_transfer(
            store, engine, backend, entities=shipment,
        ) is True
        assert _snapshot(store) == canonical

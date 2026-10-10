"""PC6: a Logistics pickup transition must use its authoritative reservation.

The legacy SimPy resource ledger must not book the same physical courier a
second time when an opt-in PostgreSQL temporal reservation owns capacity.
"""
from dataclasses import replace
from datetime import timedelta
from uuid import uuid4
import os

import pytest

from sose.backends.simpy import SimPyBackend
from sose.composition.resource_intents import IntentResourceCoordinator
from sose.core.resource_identity import ResourcePoolContract
from sose.core.resource_reservations import ResourceConflictError, TemporalReservation
from sose.examples.logistics import simulation as logistics
from sose.persistence.postgres import PostgresPersistence

DSN = os.environ.get("SOSE_TEST_POSTGRES_DSN")
pytestmark = pytest.mark.skipif(not DSN, reason="SOSE_TEST_POSTGRES_DSN required")
SLOT = timedelta(minutes=5)
POLICY = "composition.deliver_shipment"


def _pool():
    return ResourcePoolContract(
        resource_type="pickup_courier", pool_id="pc6-shared-dispatch",
        scope="shared", capacity=1,
    )


def _context():
    return PostgresPersistence(
        DSN, namespace="pc6_pickup_" + uuid4().hex[:12],
    )


def _runtime(store):
    entities = logistics.seed_reference(store)
    _, engine = logistics.build_runtime(store)
    backend = SimPyBackend(origin=logistics.ORIGIN)
    engine.rebuild_backend(backend)
    backend.run_until(logistics.PICKUP_DUE)
    assert store.entity("shipment", entities.shipment_id).state == "pickup_scheduled"
    return entities, engine, backend


def test_pg_pickup_requires_live_durable_booking_and_does_not_double_book_simpy():
    assert DSN
    with _context() as store:
        entities, engine, backend = _runtime(store)
        pool = _pool()
        fake = TemporalReservation(
            reservation_id="effect-a", address=pool.address(),
            owner_id="company-a", start_at=logistics.PICKUP_DUE,
            end_at=logistics.PICKUP_DUE + SLOT,
            causation_id="source-a",
        )
        with pytest.raises(ResourceConflictError, match="authoritative pickup"):
            logistics.reconcile_pickup(
                store, engine, backend, entities=entities,
                authoritative_pickup=fake,
            )
        assert store.entity("shipment", entities.shipment_id).state == "pickup_scheduled"

        coordinator = IntentResourceCoordinator(
            store, {POLICY: pool}, slot_duration=SLOT, retry_delay=SLOT,
        )
        assert coordinator.admit(
            effect_id="effect-a", intent_name=POLICY,
            organization_id="company-a", due_at=logistics.PICKUP_DUE,
            now=logistics.PICKUP_DUE, causation_id="source-a",
            correlation_id="correlation-a",
        )
        booking = coordinator._ledger.get("effect-a")
        with pytest.raises(ResourceConflictError, match="authoritative pickup"):
            logistics.reconcile_pickup(
                store, engine, backend, entities=entities,
                authoritative_pickup=replace(booking, owner_id="another-company"),
            )
        assert logistics.reconcile_pickup(
            store, engine, backend, entities=entities,
            authoritative_pickup=booking,
        )
        assert store.entity("shipment", entities.shipment_id).state == "picked_up"
        assert not any(
            item.resource_name == "pickup_courier"
            for item in store.resource_reservations()
        )
        coordinator.complete("effect-a")
        coordinator.complete("effect-a")
        assert coordinator._ledger.get("effect-a").status == "released"
        assert tuple(e.action for e in coordinator._ledger.events()) == (
            "reserved", "released",
        )
        assert coordinator._ledger.audit()

    # Persistent domain state and the authoritative booking survive full restart.
    with PostgresPersistence(DSN, namespace=store.namespace) as restarted:
        assert restarted.entity("shipment", entities.shipment_id).state == "picked_up"
        assert restarted.temporal_resources().get("effect-a").status == "released"


def test_pg_pickup_cannot_run_before_or_after_its_durable_occupancy_window():
    assert DSN
    with _context() as store:
        entities, engine, backend = _runtime(store)
        coordinator = IntentResourceCoordinator(
            store, {POLICY: _pool()}, slot_duration=SLOT, retry_delay=SLOT,
        )
        assert coordinator.admit(
            effect_id="future-effect", intent_name=POLICY,
            organization_id="company-a",
            due_at=logistics.PICKUP_DUE + SLOT,
            now=logistics.PICKUP_DUE,
            causation_id="source-a", correlation_id="correlation-a",
        )
        booking = coordinator._ledger.get("future-effect")
        with pytest.raises(ResourceConflictError, match="authoritative pickup"):
            logistics.reconcile_pickup(
                store, engine, backend, entities=entities,
                authoritative_pickup=booking,
            )
        assert store.entity("shipment", entities.shipment_id).state == "pickup_scheduled"
        assert store.resource_reservations() == ()
        backend.run_until(logistics.PICKUP_DUE + SLOT)
        assert logistics.reconcile_pickup(
            store, engine, backend, entities=entities,
            authoritative_pickup=booking,
        )
        coordinator.complete("future-effect")
        assert coordinator._ledger.audit()

"""PC6: a Logistics pickup transition must use its authoritative reservation.

The legacy SimPy resource ledger must not book the same physical courier a
second time when an opt-in PostgreSQL temporal reservation owns capacity.
"""
from dataclasses import replace
from datetime import timedelta
from threading import Event, Thread
from uuid import uuid4
import os

import pytest

from sose.backends.simpy import SimPyBackend
from sose.composition.resource_intents import IntentResourceCoordinator
from sose.core.events import Command
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


def _bind_effect(store, effect_id, shipment_id, *, cause="source-a"):
    with store.transaction() as uow:
        uow.save_command(Command(
            command_id=effect_id, name=POLICY, entity_type="shipment",
            entity_id=shipment_id, issued_at=logistics.PICKUP_DUE,
            due_at=logistics.PICKUP_DUE,
            causation_id=cause, correlation_id="correlation-a",
        ))


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

        _bind_effect(store, "effect-a", entities.shipment_id)
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
        assert tuple(sorted(e[3] for e in coordinator._ledger.events())) == (
            "released", "reserved",
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
        _bind_effect(store, "future-effect", entities.shipment_id)
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


def test_pg_pickup_authorization_holds_pool_lock_through_transition_commit():
    """A concurrent release cannot invalidate the booking during its use."""
    assert DSN
    ns = "pc6_pickup_lock_" + uuid4().hex[:12]
    with (
        PostgresPersistence(DSN, namespace=ns) as owner,
        PostgresPersistence(DSN, namespace=ns) as contender,
    ):
        first = IntentResourceCoordinator(
            owner, {POLICY: _pool()}, slot_duration=SLOT, retry_delay=SLOT,
        )
        second = contender.temporal_resources()
        assert first.admit(
            effect_id="owned", intent_name=POLICY,
            organization_id="company-a", due_at=logistics.PICKUP_DUE,
            now=logistics.PICKUP_DUE, causation_id="boundary-a",
            correlation_id="flow-a",
        )
        booking = first.booking_for("owned", POLICY)
        inside = Event()
        unlock = Event()
        attempted = Event()
        completed = Event()
        failures = []

        def authorized_transition():
            try:
                with owner.temporal_resources().authorize_use(
                    booking, at=logistics.PICKUP_DUE,
                ):
                    inside.set()
                    if not unlock.wait(10):
                        raise TimeoutError("authorization release not signaled")
            except BaseException as exc:
                failures.append(exc)

        def concurrent_release():
            try:
                attempted.set()
                second.release("owned", at=logistics.PICKUP_DUE + SLOT)
                completed.set()
            except BaseException as exc:
                failures.append(exc)

        first_worker = Thread(target=authorized_transition)
        second_worker = Thread(target=concurrent_release)
        first_worker.start()
        assert inside.wait(10)
        second_worker.start()
        assert attempted.wait(10)
        try:
            assert not completed.wait(0.3)
        finally:
            unlock.set()
        first_worker.join(timeout=10)
        second_worker.join(timeout=10)
        assert not first_worker.is_alive() and not second_worker.is_alive()
        assert failures == []
        assert completed.is_set()
        assert second.get("owned").status == "released"
        assert second.audit()


def test_pg_booking_for_different_shipment_cannot_authorize_pickup():
    assert DSN
    with _context() as store:
        entities, engine, backend = _runtime(store)
        _bind_effect(store, "wrong-target", "a-different-shipment")
        coordinator = IntentResourceCoordinator(
            store, {POLICY: _pool()}, slot_duration=SLOT, retry_delay=SLOT,
        )
        assert coordinator.admit(
            effect_id="wrong-target", intent_name=POLICY,
            organization_id="company-a", due_at=logistics.PICKUP_DUE,
            now=logistics.PICKUP_DUE, causation_id="source-a",
            correlation_id="correlation-a",
        )
        with pytest.raises(ResourceConflictError, match="target"):
            logistics.reconcile_pickup(
                store, engine, backend, entities=entities,
                authoritative_pickup=coordinator.booking_for("wrong-target", POLICY),
            )
        assert store.entity("shipment", entities.shipment_id).state == "pickup_scheduled"
        assert not store.resource_reservations()


def test_pg_invalid_booking_never_resumes_a_delayed_shipment():
    """The resume state transition cannot precede authoritative validation."""
    assert DSN
    with _context() as store:
        entities, engine, backend = _runtime(store)
        shipment = store.entity("shipment", entities.shipment_id)
        logistics._dispatch(
            engine, shipment, "delay",
            key=("pc6", shipment.id, "delay-before-reservation"),
        )
        assert store.entity("shipment", entities.shipment_id).state == "delayed_pickup"
        fake = TemporalReservation(
            reservation_id="not-booked", address=_pool().address(),
            owner_id="company-a", start_at=logistics.PICKUP_DUE,
            end_at=logistics.PICKUP_DUE+SLOT,
            causation_id="source-a",
        )
        events_before = tuple(store.events())
        with pytest.raises(ResourceConflictError, match="authoritative pickup"):
            logistics.reconcile_pickup(
                store, engine, backend, entities=entities,
                authoritative_pickup=fake,
            )
        assert store.entity("shipment", entities.shipment_id).state == "delayed_pickup"
        assert store.events() == events_before

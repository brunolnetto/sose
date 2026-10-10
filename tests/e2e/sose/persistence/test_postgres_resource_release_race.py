"""Regression: a recovery worker must not steal an in-flight release intent.

Two PostgreSQL connections share an authoritative namespace. One worker is
paused after persisting the release intent but before the backend release.
The other must not finalize that live writer's release concurrently.
"""
from datetime import datetime, timezone
from threading import Event, Thread
from uuid import uuid4
import os

import pytest

from sose.core.resources import DurableResourceManager
from sose.core.runtime import ResourceDefinition, ResourceReservation
from sose.persistence.postgres import PostgresPersistence

DSN = os.environ.get("SOSE_TEST_POSTGRES_DSN")
pytestmark = pytest.mark.skipif(not DSN, reason="SOSE_TEST_POSTGRES_DSN required")
T0 = datetime(2026, 10, 10, 12, tzinfo=timezone.utc)


class SuspendedBackend:
    now = T0

    def __init__(self, entered, resume):
        self.entered = entered
        self.resume = resume

    def release_resource(self, lease):
        self.entered.set()
        if not self.resume.wait(10):
            raise TimeoutError("test backend release was not resumed")


def test_pg_inflight_release_blocks_competing_recovery_of_same_reservation():
    assert DSN
    namespace = "resource_release_race_" + uuid4().hex[:12]
    with (
        PostgresPersistence(DSN, namespace=namespace) as owner,
        PostgresPersistence(DSN, namespace=namespace) as recoverer,
    ):
        reservation = ResourceReservation(
            reservation_id="booking-a", request_id="request-a",
            resource_name="pickup_courier", acquired_at=T0,
        )
        with owner.transaction() as uow:
            uow.save_resource_definition(ResourceDefinition("pickup_courier", capacity=1))
            uow.save_resource_reservation(reservation)

        manager = DurableResourceManager(owner)
        manager._backend_leases[reservation.reservation_id] = object()
        entered = Event()
        resume = Event()
        recovery_started = Event()
        recovery_done = Event()
        errors = []

        def releasing_worker():
            try:
                assert manager.release(
                    SuspendedBackend(entered, resume), reservation.reservation_id,
                )
            except BaseException as exc:
                errors.append(("release", exc))

        def recovering_worker():
            try:
                recovery_started.set()
                DurableResourceManager(recoverer)._finalize_interrupted_releases()
                recovery_done.set()
            except BaseException as exc:
                errors.append(("recovery", exc))

        first = Thread(target=releasing_worker)
        second = Thread(target=recovering_worker)
        first.start()
        assert entered.wait(10), "release intent did not become durable"
        second.start()
        assert recovery_started.wait(10)
        try:
            assert not recovery_done.wait(0.3), (
                "recovery stole an in-flight release from another worker"
            )
        finally:
            resume.set()
        first.join(timeout=10)
        second.join(timeout=10)
        assert not first.is_alive() and not second.is_alive()
        assert not errors, errors
        assert recovery_done.is_set()
        assert owner.resource_reservations() == ()
        assert owner.resource_release_intents() == ()


def test_pg_disjoint_release_ids_progress_under_an_inflight_release():
    assert DSN
    ns = "resource_release_disjoint_" + uuid4().hex[:12]
    with (
        PostgresPersistence(DSN, namespace=ns) as first_store,
        PostgresPersistence(DSN, namespace=ns) as second_store,
    ):
        with first_store.transaction() as uow:
            uow.save_resource_definition(ResourceDefinition("pickup_courier", capacity=2))
            for identifier in ("a", "b"):
                uow.save_resource_reservation(ResourceReservation(
                    reservation_id=f"booking-{identifier}",
                    request_id=f"request-{identifier}",
                    resource_name="pickup_courier", acquired_at=T0,
                ))
        started = Event()
        release_first = Event()
        failures = []
        owner = DurableResourceManager(first_store)
        contender = DurableResourceManager(second_store)
        owner._backend_leases["booking-a"] = object()
        contender._backend_leases["booking-b"] = object()

        def slow():
            try:
                assert owner.release(SuspendedBackend(started, release_first), "booking-a")
            except BaseException as exc:
                failures.append(exc)

        class ImmediateBackend:
            now = T0
            def release_resource(self, lease):
                pass

        thread = Thread(target=slow)
        thread.start()
        assert started.wait(10)
        try:
            assert contender.release(ImmediateBackend(), "booking-b")
        finally:
            release_first.set()
        thread.join(timeout=10)
        assert not thread.is_alive()
        assert not failures
        assert first_store.resource_reservations() == ()

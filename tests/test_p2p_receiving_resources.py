from sose.backends.simpy import SimPyBackend
from sose.examples.p2p.simulation import (
    ORIGIN,
    build_runtime,
    reconcile_receiving_resources,
    seed_happy_path,
)
from sose.persistence.memory import MemoryPersistence


def test_receipt_cannot_begin_receiving_until_dock_is_acquired():
    persistence = MemoryPersistence()
    ids = seed_happy_path(persistence)
    _, engine = build_runtime(persistence)
    backend = SimPyBackend(origin=ORIGIN)
    engine.rebuild_backend(backend)

    engine.resources.request(
        backend,
        resource_name="receiving_dock",
        request_id="dock-blocker",
        requested_at=ORIGIN,
        priority=100,
    )
    backend.run_until(ORIGIN)

    assert reconcile_receiving_resources(
        persistence,
        engine,
        backend,
        entities=ids,
    ) is False
    assert persistence.entity("receipt", ids.receipt_id).state == "pending"
    assert [d.request_id for d in persistence.resource_demands()] == [
        f"receiving-dock:{ids.receipt_id}"
    ]

    blocker = next(
        reservation
        for reservation in persistence.resource_reservations()
        if reservation.request_id == "dock-blocker"
    )
    engine.resources.release(backend, blocker.reservation_id)
    backend.run_until(ORIGIN)

    assert reconcile_receiving_resources(
        persistence,
        engine,
        backend,
        entities=ids,
    ) is True
    assert persistence.entity("receipt", ids.receipt_id).state == "inspected"
    assert persistence.resource_demands() == ()
    assert persistence.resource_reservations() == ()


def test_receipt_waits_for_inspector_while_holding_dock():
    persistence = MemoryPersistence()
    ids = seed_happy_path(persistence)
    _, engine = build_runtime(persistence)
    backend = SimPyBackend(origin=ORIGIN)
    engine.rebuild_backend(backend)

    engine.resources.request(
        backend,
        resource_name="inspector",
        request_id="inspector-blocker",
        requested_at=ORIGIN,
    )
    backend.run_until(ORIGIN)

    assert reconcile_receiving_resources(
        persistence,
        engine,
        backend,
        entities=ids,
    ) is False
    assert persistence.entity("receipt", ids.receipt_id).state == "receiving"

    reservations = {
        reservation.request_id for reservation in persistence.resource_reservations()
    }
    assert f"receiving-dock:{ids.receipt_id}" in reservations
    assert "inspector-blocker" in reservations
    assert [d.request_id for d in persistence.resource_demands()] == [
        f"inspector:{ids.receipt_id}"
    ]

    blocker = next(
        reservation
        for reservation in persistence.resource_reservations()
        if reservation.request_id == "inspector-blocker"
    )
    engine.resources.release(backend, blocker.reservation_id)
    backend.run_until(ORIGIN)

    assert reconcile_receiving_resources(
        persistence,
        engine,
        backend,
        entities=ids,
    ) is True
    assert persistence.entity("receipt", ids.receipt_id).state == "inspected"
    assert persistence.resource_demands() == ()
    assert persistence.resource_reservations() == ()

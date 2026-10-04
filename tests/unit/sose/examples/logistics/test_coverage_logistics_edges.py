from __future__ import annotations

from datetime import timedelta

import pytest

from sose.backends.simpy import SimPyBackend
from sose.examples.logistics.simulation import (
    ORIGIN,
    PICKUP_DUE,
    _shipment,
    delivery_attempt_id,
    ensure_delivery_attempt,
    reconcile_delivery_dispatch,
    reconcile_delivery_failure,
    reconcile_delivery_success,
    reconcile_origin_hub,
    reconcile_pickup,
    reconcile_transfer,
    seed_reference,
    build_runtime,
)
from sose.persistence.memory import MemoryPersistence


def _runtime():
    persistence = MemoryPersistence()
    entities = seed_reference(persistence)
    _, engine = build_runtime(persistence)
    backend = SimPyBackend(origin=ORIGIN)
    engine.rebuild_backend(backend)
    return persistence, entities, engine, backend


def _save(persistence, entity):
    with persistence.transaction() as uow:
        uow.save_entity(entity)


def _set_shipment_state(persistence, entities, state):
    shipment = persistence.entity("shipment", entities.shipment_id)
    assert shipment is not None
    shipment.state = state
    _save(persistence, shipment)
    return shipment


def test_delivery_attempt_id_rejects_non_positive_ordinal():
    with pytest.raises(ValueError, match="ordinal must be >= 1"):
        delivery_attempt_id(0)


def test_missing_shipment_guard_is_observable():
    persistence = MemoryPersistence()
    entities = type("Entities", (), {"shipment_id": "missing"})()

    with pytest.raises(RuntimeError, match="shipment was not persisted"):
        _shipment(persistence, entities)


def test_pickup_returns_true_for_already_progressed_shipment():
    persistence, entities, engine, backend = _runtime()
    _set_shipment_state(persistence, entities, "picked_up")

    assert reconcile_pickup(
        persistence,
        engine,
        backend,
        entities=entities,
    ) is True


def test_pickup_returns_false_for_created_shipment_before_schedule_boundary():
    persistence, entities, engine, backend = _runtime()

    assert reconcile_pickup(
        persistence,
        engine,
        backend,
        entities=entities,
    ) is False


def test_pickup_scenario_unavailability_delays_shipment(monkeypatch):
    persistence, entities, engine, backend = _runtime()
    backend.run_until(PICKUP_DUE)
    monkeypatch.setattr(
        engine.context.scenarios,
        "attribute",
        lambda name, default=True: False
        if name == "logistics.courier.available"
        else default,
    )

    assert reconcile_pickup(
        persistence,
        engine,
        backend,
        entities=entities,
    ) is False
    shipment = persistence.entity("shipment", entities.shipment_id)
    assert shipment is not None and shipment.state == "delayed_pickup"


def test_delayed_pickup_resumes_when_scenario_recovers(monkeypatch):
    persistence, entities, engine, backend = _runtime()
    backend.run_until(PICKUP_DUE)
    _set_shipment_state(persistence, entities, "delayed_pickup")
    monkeypatch.setattr(
        engine.context.scenarios,
        "attribute",
        lambda _name, default=True: default,
    )

    assert reconcile_pickup(
        persistence,
        engine,
        backend,
        entities=entities,
    ) is True
    shipment = persistence.entity("shipment", entities.shipment_id)
    assert shipment is not None and shipment.state == "picked_up"


def test_origin_hub_rejects_wrong_state_and_accepts_terminal_state():
    persistence, entities, engine, backend = _runtime()
    assert reconcile_origin_hub(
        persistence,
        engine,
        backend,
        entities=entities,
    ) is False

    _set_shipment_state(persistence, entities, "at_origin_hub")
    assert reconcile_origin_hub(
        persistence,
        engine,
        backend,
        entities=entities,
    ) is True


def test_origin_hub_waits_when_dock_capacity_is_contended():
    persistence, entities, engine, backend = _runtime()
    _set_shipment_state(persistence, entities, "picked_up")

    blocker = engine.resources.ensure_requested(
        backend,
        resource_name="origin_dock",
        request_id="origin-dock:blocker",
        requested_at=backend.now,
    )
    assert blocker is not None

    assert reconcile_origin_hub(
        persistence,
        engine,
        backend,
        entities=entities,
    ) is False
    demands = {d.request_id for d in persistence.resource_demands()}
    assert f"origin-dock:{entities.shipment_id}" in demands


def test_transfer_rejects_unrelated_state_and_accepts_destination_state():
    persistence, entities, engine, backend = _runtime()
    _set_shipment_state(persistence, entities, "created")
    assert reconcile_transfer(
        persistence,
        engine,
        backend,
        entities=entities,
    ) is False

    _set_shipment_state(persistence, entities, "at_destination_hub")
    assert reconcile_transfer(
        persistence,
        engine,
        backend,
        entities=entities,
    ) is True


def test_transfer_waits_when_transfer_vehicle_is_contended():
    persistence, entities, engine, backend = _runtime()
    _set_shipment_state(persistence, entities, "at_origin_hub")
    blocker = engine.resources.ensure_requested(
        backend,
        resource_name="transfer_vehicle",
        request_id="transfer:blocker",
        requested_at=backend.now,
    )
    assert blocker is not None

    assert reconcile_transfer(
        persistence,
        engine,
        backend,
        entities=entities,
    ) is False
    assert any(
        demand.request_id == f"transfer-vehicle:{entities.shipment_id}"
        for demand in persistence.resource_demands()
    )


def test_transfer_waits_when_destination_dock_is_contended():
    persistence, entities, engine, backend = _runtime()
    _set_shipment_state(persistence, entities, "in_transfer")
    blocker = engine.resources.ensure_requested(
        backend,
        resource_name="destination_dock",
        request_id="destination:blocker",
        requested_at=backend.now,
    )
    assert blocker is not None

    assert reconcile_transfer(
        persistence,
        engine,
        backend,
        entities=entities,
    ) is False
    assert any(
        demand.request_id == f"destination-dock:{entities.shipment_id}"
        for demand in persistence.resource_demands()
    )


def test_ensure_delivery_attempt_is_idempotent():
    persistence, entities, engine, _ = _runtime()

    first = ensure_delivery_attempt(persistence, engine, ordinal=1)
    second = ensure_delivery_attempt(persistence, engine, ordinal=1)

    assert first == second
    assert first.id == delivery_attempt_id(1)


def test_delivery_dispatch_rejects_wrong_shipment_state():
    persistence, entities, engine, backend = _runtime()

    assert reconcile_delivery_dispatch(
        persistence,
        engine,
        backend,
        entities=entities,
        ordinal=1,
    ) is False


def test_delivery_dispatch_returns_true_for_active_attempt():
    persistence, entities, engine, backend = _runtime()
    _set_shipment_state(persistence, entities, "out_for_delivery")
    attempt = ensure_delivery_attempt(persistence, engine, ordinal=1)
    attempt.state = "out_for_delivery"
    _save(persistence, attempt)

    assert reconcile_delivery_dispatch(
        persistence,
        engine,
        backend,
        entities=entities,
        ordinal=1,
    ) is True


def test_delivery_dispatch_rejects_non_pending_attempt():
    persistence, entities, engine, backend = _runtime()
    _set_shipment_state(persistence, entities, "out_for_delivery")
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


def test_delivery_dispatch_scenario_unavailability_delays_at_destination(monkeypatch):
    persistence, entities, engine, backend = _runtime()
    _set_shipment_state(persistence, entities, "at_destination_hub")
    monkeypatch.setattr(
        engine.context.scenarios,
        "attribute",
        lambda name, default=True: False
        if name == "logistics.courier.available"
        else default,
    )

    assert reconcile_delivery_dispatch(
        persistence,
        engine,
        backend,
        entities=entities,
        ordinal=1,
    ) is False
    shipment = persistence.entity("shipment", entities.shipment_id)
    assert shipment is not None and shipment.state == "delayed_destination_hub"


def test_delivery_dispatch_waits_when_delivery_courier_is_contended():
    persistence, entities, engine, backend = _runtime()
    _set_shipment_state(persistence, entities, "at_destination_hub")
    blocker = engine.resources.ensure_requested(
        backend,
        resource_name="delivery_courier",
        request_id="delivery:blocker",
        requested_at=backend.now,
    )
    assert blocker is not None

    assert reconcile_delivery_dispatch(
        persistence,
        engine,
        backend,
        entities=entities,
        ordinal=1,
    ) is False
    attempt = ensure_delivery_attempt(persistence, engine, ordinal=1)
    assert any(
        demand.request_id == f"delivery-courier:{attempt.id}"
        for demand in persistence.resource_demands()
    )


@pytest.mark.parametrize("helper", [reconcile_delivery_success, reconcile_delivery_failure])
def test_delivery_completion_helpers_require_active_attempt(helper):
    persistence, entities, engine, backend = _runtime()
    _set_shipment_state(persistence, entities, "out_for_delivery")

    with pytest.raises(RuntimeError, match="delivery attempt is not active"):
        helper(
            persistence,
            engine,
            backend,
            entities=entities,
            ordinal=1,
        )


@pytest.mark.parametrize("helper", [reconcile_delivery_success, reconcile_delivery_failure])
def test_delivery_completion_helpers_require_out_for_delivery_shipment(helper):
    persistence, entities, engine, backend = _runtime()
    attempt = ensure_delivery_attempt(persistence, engine, ordinal=1)
    attempt.state = "out_for_delivery"
    _save(persistence, attempt)
    _set_shipment_state(persistence, entities, "at_destination_hub")

    with pytest.raises(RuntimeError, match="shipment is not out for delivery"):
        helper(
            persistence,
            engine,
            backend,
            entities=entities,
            ordinal=1,
        )


def test_delivery_failure_schedules_requested_retry_delay():
    persistence, entities, engine, backend = _runtime()
    _set_shipment_state(persistence, entities, "out_for_delivery")
    attempt = ensure_delivery_attempt(persistence, engine, ordinal=1)
    attempt.state = "out_for_delivery"
    _save(persistence, attempt)

    due_at = reconcile_delivery_failure(
        persistence,
        engine,
        backend,
        entities=entities,
        ordinal=1,
        retry_after=timedelta(minutes=17),
    )

    assert due_at == backend.now + timedelta(minutes=17)
    pending = engine.scheduler.find_pending(
        entity_type="shipment",
        entity_id=entities.shipment_id,
        name="resume",
    )
    assert pending is not None and pending.work.due_at == due_at

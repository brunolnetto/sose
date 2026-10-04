from __future__ import annotations

from datetime import timedelta

import pytest

from sose.backends.simpy import SimPyBackend
from sose.examples.telecom import simulation as telecom_simulation
from sose.examples.telecom.simulation import (
    ORIGIN,
    _entity,
    acknowledge_incident,
    build_runtime,
    ensure_fulfillment_entities,
    raise_service_alarm,
    reconcile_activation,
    record_usage,
    restore_service,
    schedule_activation,
    seed_reference,
    service_order_id,
    subscription_service_id,
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


def _scheduled_activation():
    persistence, entities, engine, backend = _runtime()
    due_at = schedule_activation(
        persistence,
        engine,
        backend,
        entities=entities,
    )
    return persistence, entities, engine, backend, due_at


def _active_service():
    persistence, entities, engine, backend, due_at = _scheduled_activation()
    backend.run_until(due_at)
    assert reconcile_activation(
        persistence,
        engine,
        backend,
        entities=entities,
    )
    return persistence, entities, engine, backend


def test_missing_telecom_entity_guard_is_observable():
    with pytest.raises(RuntimeError, match="was not persisted"):
        _entity(MemoryPersistence(), "telecom_product_order", "missing")


def test_fulfillment_rejects_terminal_invalid_product_order():
    persistence, entities, engine, _ = _runtime()
    order = persistence.entity("telecom_product_order", entities.product_order_id)
    assert order is not None
    order.state = "cancelled"
    _save(persistence, order)

    with pytest.raises(RuntimeError, match="order cannot be fulfilled"):
        ensure_fulfillment_entities(
            persistence,
            engine,
            entities=entities,
        )


def test_fulfillment_creation_is_idempotent():
    persistence, entities, engine, _ = _runtime()

    first_order, first_service = ensure_fulfillment_entities(
        persistence,
        engine,
        entities=entities,
    )
    count_before = len(persistence.entities())
    second_order, second_service = ensure_fulfillment_entities(
        persistence,
        engine,
        entities=entities,
    )

    assert second_order.id == first_order.id == service_order_id(entities.product_order_id)
    assert second_service.id == first_service.id == subscription_service_id(
        entities.product_order_id
    )
    assert len(persistence.entities()) == count_before


def test_activation_schedule_replay_does_not_duplicate_work():
    persistence, entities, engine, backend = _runtime()

    first = schedule_activation(
        persistence,
        engine,
        backend,
        entities=entities,
        delay=timedelta(hours=2),
    )
    second = schedule_activation(
        persistence,
        engine,
        backend,
        entities=entities,
        delay=timedelta(hours=9),
    )

    assert second == first
    assert len(persistence.scheduled_work()) == 1


def test_activation_schedule_returns_now_for_activation_ready_service():
    persistence, entities, engine, backend, due_at = _scheduled_activation()
    backend.run_until(due_at)

    assert schedule_activation(
        persistence,
        engine,
        backend,
        entities=entities,
    ) == backend.now
    assert persistence.scheduled_work() == ()


def test_activation_schedule_returns_now_for_active_service_without_new_work():
    persistence, entities, engine, backend = _active_service()

    assert schedule_activation(
        persistence,
        engine,
        backend,
        entities=entities,
    ) == backend.now
    assert persistence.scheduled_work() == ()


def test_activation_schedule_rejects_invalid_service_state():
    persistence, entities, engine, backend = _runtime()
    ensure_fulfillment_entities(persistence, engine, entities=entities)
    service = persistence.entity(
        "telecom_subscription_service",
        subscription_service_id(entities.product_order_id),
    )
    assert service is not None
    service.state = "suspended"
    _save(persistence, service)

    with pytest.raises(RuntimeError, match="cannot schedule activation"):
        schedule_activation(
            persistence,
            engine,
            backend,
            entities=entities,
        )


def test_activation_returns_false_without_fulfillment_entities():
    persistence, entities, engine, backend = _runtime()

    assert reconcile_activation(
        persistence,
        engine,
        backend,
        entities=entities,
    ) is False


def test_activation_returns_false_before_activation_ready():
    persistence, entities, engine, backend = _runtime()
    ensure_fulfillment_entities(persistence, engine, entities=entities)

    assert reconcile_activation(
        persistence,
        engine,
        backend,
        entities=entities,
    ) is False


def test_activation_outage_does_not_own_worker_capacity(monkeypatch):
    persistence, entities, engine, backend, due_at = _scheduled_activation()
    backend.run_until(due_at)
    service = persistence.entity(
        "telecom_subscription_service",
        subscription_service_id(entities.product_order_id),
    )
    assert service is not None and service.state == "activation_ready"

    monkeypatch.setattr(
        engine.context.scenarios,
        "attribute",
        lambda name, default=True: False
        if name == "telecom.provisioning.available"
        else default,
    )

    assert reconcile_activation(
        persistence,
        engine,
        backend,
        entities=entities,
    ) is False
    request_id = f"provisioning-worker:{service.id}"
    assert engine.resources.has_request(request_id) is False
    persisted = persistence.entity("telecom_subscription_service", service.id)
    assert persisted is not None and persisted.state == "activation_ready"


def test_activation_waits_with_durable_worker_demand():
    persistence, entities, engine, backend, due_at = _scheduled_activation()
    backend.run_until(due_at)
    service = persistence.entity(
        "telecom_subscription_service",
        subscription_service_id(entities.product_order_id),
    )
    assert service is not None

    blocker = engine.resources.ensure_requested(
        backend,
        resource_name="provisioning_worker",
        request_id="provisioning:blocker",
        requested_at=backend.now,
    )
    assert blocker is not None

    assert reconcile_activation(
        persistence,
        engine,
        backend,
        entities=entities,
    ) is False
    request_id = f"provisioning-worker:{service.id}"
    assert engine.resources.has_request(request_id)
    assert engine.resources.reservation_for(request_id) is None


def test_active_service_replay_completes_dangling_order_state():
    persistence, entities, engine, backend, due_at = _scheduled_activation()
    backend.run_until(due_at)
    service = persistence.entity(
        "telecom_subscription_service",
        subscription_service_id(entities.product_order_id),
    )
    assert service is not None
    service.state = "active"
    _save(persistence, service)

    service_order = persistence.entity(
        "telecom_service_order",
        service_order_id(entities.product_order_id),
    )
    order = persistence.entity("telecom_product_order", entities.product_order_id)
    assert service_order is not None and order is not None
    assert service_order.state == "provisioning"
    assert order.state == "in_progress"

    assert reconcile_activation(
        persistence,
        engine,
        backend,
        entities=entities,
    )
    assert persistence.entity("telecom_service_order", service_order.id).state == "completed"
    assert persistence.entity("telecom_product_order", order.id).state == "completed"


@pytest.mark.parametrize(
    ("sequence", "quantity", "message"),
    [
        (0, 1.0, "sequence must be positive"),
        (-1, 1.0, "sequence must be positive"),
        (1, 0.0, "finite and positive"),
        (1, -1.0, "finite and positive"),
    ],
)
def test_usage_validates_sequence_and_positive_quantity(sequence, quantity, message):
    persistence, entities, engine, _ = _active_service()

    with pytest.raises(ValueError, match=message):
        record_usage(
            persistence,
            engine,
            entities=entities,
            sequence=sequence,
            quantity=quantity,
        )


def test_usage_identity_rejects_unit_conflict():
    persistence, entities, engine, _ = _active_service()
    first = record_usage(
        persistence,
        engine,
        entities=entities,
        sequence=5,
        quantity=10.0,
        unit="MB",
    )
    assert first.state == "committed"

    with pytest.raises(ValueError, match="different measurement"):
        record_usage(
            persistence,
            engine,
            entities=entities,
            sequence=5,
            quantity=10.0,
            unit="GB",
        )


def test_alarm_validates_incident_key():
    persistence, entities, engine, _ = _active_service()

    with pytest.raises(ValueError, match="incident_key"):
        raise_service_alarm(
            persistence,
            engine,
            entities=entities,
            incident_key="",
        )


def test_alarm_requires_provisioned_service():
    persistence, entities, engine, _ = _runtime()

    with pytest.raises(RuntimeError, match="provisioned subscription service"):
        raise_service_alarm(
            persistence,
            engine,
            entities=entities,
            incident_key="outage",
        )


def test_alarm_replay_does_not_duplicate_open_incident():
    persistence, entities, engine, _ = _active_service()

    first_alarm, first_ticket = raise_service_alarm(
        persistence,
        engine,
        entities=entities,
        incident_key="incident-a",
    )
    second_alarm, second_ticket = raise_service_alarm(
        persistence,
        engine,
        entities=entities,
        incident_key="incident-a",
    )
    service = persistence.entity(
        "telecom_subscription_service",
        subscription_service_id(entities.product_order_id),
    )

    assert second_alarm.id == first_alarm.id
    assert second_ticket.id == first_ticket.id
    assert service is not None
    assert service.attributes["open_incident_keys"] == ["incident-a"]


def test_acknowledge_incident_requires_provisioned_service():
    persistence, entities, engine, _ = _runtime()

    with pytest.raises(RuntimeError, match="service was not provisioned"):
        acknowledge_incident(
            persistence,
            engine,
            entities=entities,
            incident_key="missing",
        )


def test_restore_service_returns_false_without_provisioned_service():
    persistence, entities, engine, _ = _runtime()

    assert restore_service(
        persistence,
        engine,
        entities=entities,
        incident_key="missing",
    ) is False


def test_acknowledge_incident_is_idempotent_after_acknowledgement():
    persistence, entities, engine, _ = _active_service()
    raise_service_alarm(
        persistence,
        engine,
        entities=entities,
        incident_key="incident-b",
    )
    acknowledge_incident(
        persistence,
        engine,
        entities=entities,
        incident_key="incident-b",
    )
    events_before = tuple(persistence.events())

    acknowledge_incident(
        persistence,
        engine,
        entities=entities,
        incident_key="incident-b",
    )

    assert tuple(persistence.events()) == events_before


def test_restore_closed_incident_is_idempotent():
    persistence, entities, engine, _ = _active_service()
    raise_service_alarm(
        persistence,
        engine,
        entities=entities,
        incident_key="incident-c",
    )
    assert restore_service(
        persistence,
        engine,
        entities=entities,
        incident_key="incident-c",
    )
    events_before = tuple(persistence.events())

    assert restore_service(
        persistence,
        engine,
        entities=entities,
        incident_key="incident-c",
    )
    assert tuple(persistence.events()) == events_before


def test_complete_activation_helper_skips_dispatch_for_terminal_states(monkeypatch):
    persistence, entities, engine, _ = _active_service()
    service_order = persistence.entity(
        "telecom_service_order",
        service_order_id(entities.product_order_id),
    )
    assert service_order is not None and service_order.state == "completed"
    calls: list[str] = []
    monkeypatch.setattr(
        telecom_simulation,
        "_dispatch",
        lambda _engine, _entity, event, **kwargs: calls.append(event),
    )

    telecom_simulation._complete_activation_if_active(
        persistence,
        engine,
        entities=entities,
        service_order=service_order,
        correlation_id="corr",
    )

    assert calls == []


def test_activate_service_helper_returns_early_when_service_not_activation_ready(monkeypatch):
    persistence, entities, engine, _ = _active_service()
    service_id = subscription_service_id(entities.product_order_id)
    calls: list[str] = []
    monkeypatch.setattr(
        telecom_simulation,
        "_dispatch",
        lambda _engine, _entity, event, **kwargs: calls.append(event),
    )

    telecom_simulation._activate_service_if_ready(
        persistence,
        engine,
        service_id=service_id,
        correlation_id="corr",
    )

    assert calls == []


def test_drop_open_incident_returns_service_when_resolution_is_incomplete():
    persistence, entities, _engine, _ = _active_service()
    service = _entity(
        persistence,
        "telecom_subscription_service",
        subscription_service_id(entities.product_order_id),
    )
    alarm = type("Alarm", (), {"state": "active"})()
    ticket = type("Ticket", (), {"state": "open"})()

    result = telecom_simulation._drop_open_incident_if_resolved(
        persistence,
        service=service,
        incident_key="incident-x",
        alarm=alarm,
        ticket=ticket,
    )

    assert result is service


def test_run_happy_path_raises_when_activation_cannot_be_reconciled(monkeypatch):
    monkeypatch.setattr(telecom_simulation, "reconcile_activation", lambda *args, **kwargs: False)

    with pytest.raises(RuntimeError, match="telecom activation failed"):
        telecom_simulation.run_happy_path()

import pytest

from sose.backends.simpy import SimPyBackend
from sose.examples.telecom.simulation import (
    ORIGIN,
    acknowledge_incident,
    alarm_id,
    build_runtime,
    raise_service_alarm,
    reconcile_activation,
    record_usage,
    restore_service,
    schedule_activation,
    seed_reference,
    subscription_service_id,
    trouble_ticket_id,
)
from sose.persistence.memory import MemoryPersistence


def _active_service():
    persistence = MemoryPersistence()
    entities = seed_reference(persistence)
    _, engine = build_runtime(persistence)
    backend = SimPyBackend(origin=ORIGIN)
    engine.rebuild_backend(backend)
    due_at = schedule_activation(
        persistence,
        engine,
        backend,
        entities=entities,
    )
    backend.run_until(due_at)
    assert reconcile_activation(
        persistence,
        engine,
        backend,
        entities=entities,
    )
    return persistence, entities, engine, backend


def test_usage_requires_active_service():
    persistence = MemoryPersistence()
    entities = seed_reference(persistence)
    _, engine = build_runtime(persistence)

    with pytest.raises(RuntimeError, match="active subscription"):
        record_usage(
            persistence,
            engine,
            entities=entities,
            sequence=1,
            quantity=1.0,
        )


def test_duplicate_usage_identity_is_idempotent_but_conflict_is_rejected():
    persistence, entities, engine, _ = _active_service()

    first = record_usage(
        persistence,
        engine,
        entities=entities,
        sequence=7,
        quantity=100.0,
    )
    second = record_usage(
        persistence,
        engine,
        entities=entities,
        sequence=7,
        quantity=100.0,
    )
    assert first.id == second.id

    with pytest.raises(ValueError, match="different measurement"):
        record_usage(
            persistence,
            engine,
            entities=entities,
            sequence=7,
            quantity=101.0,
        )


def test_alarm_suspends_service_and_ticket_restoration_is_explicit():
    persistence, entities, engine, _ = _active_service()
    service_id = subscription_service_id(entities.product_order_id)

    alarm, ticket = raise_service_alarm(
        persistence,
        engine,
        entities=entities,
        incident_key="ran-site-17",
        severity="major",
    )
    service = persistence.entity("telecom_subscription_service", service_id)
    assert service is not None and service.state == "suspended"
    assert alarm.id == alarm_id(service.id, "ran-site-17")
    assert ticket.id == trouble_ticket_id(service.id, "ran-site-17")

    acknowledge_incident(
        persistence,
        engine,
        entities=entities,
        incident_key="ran-site-17",
    )
    assert persistence.entity("telecom_network_alarm", alarm.id).state == "acknowledged"
    assert persistence.entity("telecom_trouble_ticket", ticket.id).state == "acknowledged"

    assert restore_service(
        persistence,
        engine,
        entities=entities,
        incident_key="ran-site-17",
    )
    assert persistence.entity("telecom_network_alarm", alarm.id).state == "cleared"
    assert persistence.entity("telecom_trouble_ticket", ticket.id).state == "closed"
    assert persistence.entity("telecom_subscription_service", service.id).state == "active"

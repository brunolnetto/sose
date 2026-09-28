from sose.backends.simpy import SimPyBackend
from sose.examples.telecom.entities import UsageRecord
from sose.examples.telecom.simulation import (
    ORIGIN,
    build_runtime,
    reconcile_activation,
    record_usage,
    schedule_activation,
    seed_reference,
    subscription_service_id,
    usage_record_id,
)
from sose.persistence.memory import MemoryPersistence
from sose.testing.restart import restart_reference_runtime


def _scheduled_activation():
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
    return persistence, entities, engine, backend, due_at


def test_activation_schedule_survives_restart():
    persistence, entities, _, backend, due_at = _scheduled_activation()

    rebuilt = restart_reference_runtime(
        persistence,
        build_runtime,
        backend,
        backend_factory=SimPyBackend,
    )
    assert len(persistence.scheduled_work()) == 1

    rebuilt.backend.run_until(due_at)
    service = persistence.entity(
        "telecom_subscription_service",
        subscription_service_id(entities.product_order_id),
    )
    assert service is not None and service.state == "activation_ready"
    assert reconcile_activation(
        persistence,
        rebuilt.engine,
        rebuilt.backend,
        entities=entities,
    )
    assert persistence.entity(
        "telecom_subscription_service",
        service.id,
    ).state == "active"
    assert persistence.scheduled_work() == ()


def test_committed_usage_is_idempotent_after_restart():
    persistence, entities, engine, backend, due_at = _scheduled_activation()
    backend.run_until(due_at)
    assert reconcile_activation(
        persistence,
        engine,
        backend,
        entities=entities,
    )
    service = persistence.entity(
        "telecom_subscription_service",
        subscription_service_id(entities.product_order_id),
    )
    assert service is not None
    first = record_usage(
        persistence,
        engine,
        entities=entities,
        sequence=42,
        quantity=2048.0,
    )

    rebuilt = restart_reference_runtime(
        persistence,
        build_runtime,
        backend,
        backend_factory=SimPyBackend,
    )
    second = record_usage(
        persistence,
        rebuilt.engine,
        entities=entities,
        sequence=42,
        quantity=2048.0,
    )
    assert first.id == second.id == usage_record_id(service.id, 42)
    assert persistence.entity("telecom_usage_record", first.id).state == "committed"



def test_captured_usage_resumes_commit_after_restart():
    persistence, entities, engine, backend, due_at = _scheduled_activation()
    backend.run_until(due_at)
    assert reconcile_activation(
        persistence,
        engine,
        backend,
        entities=entities,
    )
    service = persistence.entity(
        "telecom_subscription_service",
        subscription_service_id(entities.product_order_id),
    )
    assert service is not None

    usage = engine.context.entities.create(
        UsageRecord,
        key=("telecom-reference", service.id, "usage", 77),
        state="captured",
        attributes={
            "service_id": service.id,
            "sequence": 77,
            "quantity": 300.0,
            "unit": "MB",
            "rated": False,
        },
    )
    with persistence.transaction() as uow:
        uow.save_entity(usage)

    rebuilt = restart_reference_runtime(
        persistence,
        build_runtime,
        backend,
        backend_factory=SimPyBackend,
    )
    recovered = record_usage(
        persistence,
        rebuilt.engine,
        entities=entities,
        sequence=77,
        quantity=300.0,
    )

    assert recovered.id == usage_record_id(service.id, 77)
    assert recovered.state == "committed"

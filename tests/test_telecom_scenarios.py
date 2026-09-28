from sose.backends.simpy import SimPyBackend
from sose.examples.telecom.scenarios import provisioning_outage_scenario
from sose.examples.telecom.simulation import (
    ORIGIN,
    build_runtime,
    reconcile_activation,
    schedule_activation,
    seed_reference,
    subscription_service_id,
)
from sose.persistence.memory import MemoryPersistence


def test_provisioning_outage_defers_activation_without_fake_resource_demand():
    persistence = MemoryPersistence()
    entities = seed_reference(persistence)
    context, engine = build_runtime(
        persistence,
        scenarios=(provisioning_outage_scenario(),),
    )
    backend = SimPyBackend(origin=ORIGIN)
    engine.rebuild_backend(backend)

    due_at = schedule_activation(
        persistence,
        engine,
        backend,
        entities=entities,
    )
    engine.advance_tick()
    backend.run_until(due_at)

    service = persistence.entity(
        "telecom_subscription_service",
        subscription_service_id(entities.product_order_id),
    )
    assert service is not None and service.state == "activation_ready"
    assert reconcile_activation(
        persistence,
        engine,
        backend,
        entities=entities,
    ) is False
    assert persistence.resource_demands() == ()

    for _ in range(4):
        engine.advance_tick()
    backend.run_until(context.clock.now)

    assert reconcile_activation(
        persistence,
        engine,
        backend,
        entities=entities,
    )
    service = persistence.entity("telecom_subscription_service", service.id)
    assert service is not None and service.state == "active"

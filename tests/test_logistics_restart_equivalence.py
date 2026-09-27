from sose.backends.simpy import SimPyBackend
from sose.examples.logistics.simulation import (
    ORIGIN,
    PICKUP_DUE,
    build_runtime,
    delivery_attempt_id,
    reconcile_delivery_dispatch,
    reconcile_delivery_failure,
    reconcile_delivery_success,
    reconcile_origin_hub,
    reconcile_pickup,
    reconcile_transfer,
    seed_reference,
)
from sose.persistence.memory import MemoryPersistence
from sose.testing.restart import restart_reference_runtime


def _progress_to_retry_wait(persistence):
    entities = seed_reference(persistence)
    _, engine = build_runtime(persistence)
    backend = SimPyBackend(origin=ORIGIN)
    engine.rebuild_backend(backend)
    backend.run_until(PICKUP_DUE)

    assert reconcile_pickup(persistence, engine, backend, entities=entities)
    assert reconcile_origin_hub(persistence, engine, backend, entities=entities)
    assert reconcile_transfer(persistence, engine, backend, entities=entities)
    assert reconcile_delivery_dispatch(
        persistence, engine, backend, entities=entities, ordinal=1
    )
    retry_at = reconcile_delivery_failure(
        persistence, engine, backend, entities=entities, ordinal=1
    )
    return entities, engine, backend, retry_at


def _snapshot(persistence, entities):
    return {
        "shipment": persistence.entity("shipment", entities.shipment_id),
        "attempt1": persistence.entity("delivery_attempt", delivery_attempt_id(1)),
        "attempt2": persistence.entity("delivery_attempt", delivery_attempt_id(2)),
        "events": persistence.events(),
        "scheduled": persistence.scheduled_work(),
        "position": persistence.simulation_position(),
        "resources": persistence.resource_reservations(),
        "demands": persistence.resource_demands(),
        "release_intents": persistence.resource_release_intents(),
        "store_items": persistence.store_items(),
        "store_puts": persistence.store_put_intents(),
        "store_gets": persistence.store_get_requests(),
        "store_results": persistence.store_get_results(),
    }


def _finish_retry(persistence, entities, engine, backend, retry_at):
    backend.run_until(retry_at)
    assert reconcile_delivery_dispatch(
        persistence, engine, backend, entities=entities, ordinal=2
    )
    reconcile_delivery_success(
        persistence, engine, backend, entities=entities, ordinal=2
    )


def test_failed_retry_path_is_restart_equivalent():
    continuous = MemoryPersistence()
    c_entities, c_engine, c_backend, c_retry_at = _progress_to_retry_wait(continuous)
    _finish_retry(
        continuous, c_entities, c_engine, c_backend, c_retry_at
    )

    restarted = MemoryPersistence()
    r_entities, _, _, r_retry_at = _progress_to_retry_wait(restarted)
    position = restarted.simulation_position()
    assert position is not None

    _, rebuilt_engine = build_runtime(
        restarted,
        now=position.logical_time,
        tick=position.logical_tick,
    )
    rebuilt_backend = SimPyBackend(origin=position.logical_time)
    rebuilt_engine.rebuild_backend(rebuilt_backend)
    _finish_retry(
        restarted,
        r_entities,
        rebuilt_engine,
        rebuilt_backend,
        r_retry_at,
    )

    assert _snapshot(restarted, r_entities) == _snapshot(continuous, c_entities)
    assert restarted.entity("delivery_attempt", delivery_attempt_id(1)).state == "failed"
    assert restarted.entity("delivery_attempt", delivery_attempt_id(2)).state == "delivered"

from sose.backends.simpy import SimPyBackend
from sose.examples.cards_payments.simulation import (
    ORIGIN,
    build_runtime,
    reconcile_authorization,
    reconcile_capture_and_schedule_settlement,
    reconcile_settlement,
    seed_reference,
)
from sose.persistence.memory import MemoryPersistence


def _progress_to_retry_wait(persistence):
    entities = seed_reference(persistence)
    _, engine = build_runtime(persistence)
    backend = SimPyBackend(origin=ORIGIN)
    engine.rebuild_backend(backend)

    assert reconcile_authorization(
        persistence, engine, backend, entities=entities
    )
    settlement_at = reconcile_capture_and_schedule_settlement(
        persistence, engine, backend, entities=entities
    )
    backend.run_until(settlement_at)
    retry_at = reconcile_settlement(
        persistence, engine, backend, entities=entities, outcome="retry"
    )
    assert retry_at is not None
    assert persistence.entity(
        "card_payment", entities.payment_id
    ).state == "settlement_retry_wait"
    return entities, engine, backend, retry_at


def _snapshot(persistence, entities):
    return {
        "payment": persistence.entity("card_payment", entities.payment_id),
        "events": persistence.events(),
        "scheduled": persistence.scheduled_work(),
        "position": persistence.simulation_position(),
        "resource_demands": persistence.resource_demands(),
        "resource_reservations": persistence.resource_reservations(),
        "release_intents": persistence.resource_release_intents(),
        "scenario": persistence.scenario_state(),
    }


def _finish(persistence, entities, engine, backend, retry_at):
    backend.run_until(retry_at)
    reconcile_settlement(
        persistence, engine, backend, entities=entities, outcome="success"
    )


def test_settlement_retry_is_restart_equivalent():
    continuous = MemoryPersistence()
    c_entities, c_engine, c_backend, c_retry_at = _progress_to_retry_wait(
        continuous
    )
    _finish(
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
    _finish(
        restarted,
        r_entities,
        rebuilt_engine,
        rebuilt_backend,
        r_retry_at,
    )

    assert _snapshot(restarted, r_entities) == _snapshot(
        continuous, c_entities
    )
    assert restarted.entity("card_payment", r_entities.payment_id).state == "settled"

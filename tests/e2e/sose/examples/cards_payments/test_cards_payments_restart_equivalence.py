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
from sose.testing.restart import restart_reference_runtime


REFERENCE_OWNER_ID = "cards-payments-reference-organization"

def _progress_to_retry_wait(persistence):
    entities = seed_reference(persistence, owner_id=REFERENCE_OWNER_ID)
    _, engine = build_runtime(persistence, owner_id=REFERENCE_OWNER_ID)
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
        "position": persistence.simulation_position(owner_id=REFERENCE_OWNER_ID),
        "resource_demands": persistence.resource_demands(),
        "resource_reservations": persistence.resource_reservations(),
        "release_intents": persistence.resource_release_intents(),
        "scenario": persistence.scenario_state(owner_id=REFERENCE_OWNER_ID),
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
    position = restarted.simulation_position(owner_id=REFERENCE_OWNER_ID)
    assert position is not None

    rebuilt = restart_reference_runtime(
        restarted,
        build_runtime,
        owner_id=REFERENCE_OWNER_ID,
        backend_factory=SimPyBackend,
    )
    rebuilt_engine = rebuilt.engine
    rebuilt_backend = rebuilt.backend
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

from datetime import timedelta

from sose.backends.simpy import SimPyBackend
from sose.examples.subscription_saas.scenarios import ORIGIN
from sose.examples.subscription_saas.simulation import (
    build_runtime,
    reconcile_plan_change,
    request_cancellation,
    request_plan_change,
    seed_reference,
)
from sose.persistence.memory import MemoryPersistence
from sose.testing.restart import restart_reference_runtime


def test_applied_change_reconciles_after_restart_without_duplicate_entitlement():
    persistence = MemoryPersistence()
    entities = seed_reference(persistence)
    _, engine = build_runtime(persistence)
    backend = SimPyBackend(origin=ORIGIN)
    engine.rebuild_backend(backend)

    change = request_plan_change(
        persistence,
        engine,
        backend,
        entities=entities,
        ordinal=1,
        target_plan="pro",
        effective_at=ORIGIN + timedelta(days=5),
    )
    backend.run_until(ORIGIN + timedelta(days=5))

    rebuilt = restart_reference_runtime(
        persistence,
        build_runtime,
        backend,
        backend_factory=SimPyBackend,
    )
    first = reconcile_plan_change(
        persistence,
        rebuilt.engine,
        entities=entities,
        change_id=change.id,
    )
    second = reconcile_plan_change(
        persistence,
        rebuilt.engine,
        entities=entities,
        change_id=change.id,
    )

    subscription = persistence.entity("saas_subscription", entities.subscription_id)
    assert first is not None and second is not None and first.id == second.id
    assert subscription is not None
    assert len(subscription.attributes["entitlement_ids"]) == 2
    assert len(subscription.attributes["occurrence_ids"]) == 1


def test_period_end_cancellation_boundaries_survive_restart():
    persistence = MemoryPersistence()
    entities = seed_reference(persistence)
    _, engine = build_runtime(persistence)
    backend = SimPyBackend(origin=ORIGIN)
    engine.rebuild_backend(backend)

    term_end = request_cancellation(
        persistence,
        engine,
        entities=entities,
    )
    rebuilt = restart_reference_runtime(
        persistence,
        build_runtime,
        backend,
        backend_factory=SimPyBackend,
    )
    rebuilt.backend.run_until(term_end)

    subscription = persistence.entity("saas_subscription", entities.subscription_id)
    entitlement = persistence.entity(
        "saas_entitlement",
        subscription.attributes["active_entitlement_id"],
    )
    assert subscription is not None and subscription.state == "ended"
    assert entitlement is not None and entitlement.state == "revoked"
    assert persistence.scheduled_work() == ()

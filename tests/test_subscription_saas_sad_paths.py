from datetime import timedelta

import pytest

from sose.backends.simpy import SimPyBackend
from sose.examples.subscription_saas.scenarios import ORIGIN
from sose.examples.subscription_saas.simulation import (
    build_runtime,
    request_cancellation,
    request_plan_change,
    seed_reference,
    withdraw_cancellation,
)
from sose.persistence.memory import MemoryPersistence


def _runtime():
    persistence = MemoryPersistence()
    entities = seed_reference(persistence)
    _, engine = build_runtime(persistence)
    backend = SimPyBackend(origin=ORIGIN)
    engine.rebuild_backend(backend)
    return persistence, entities, engine, backend


def test_only_one_future_plan_change_can_own_effective_boundary():
    persistence, entities, engine, backend = _runtime()
    request_plan_change(
        persistence,
        engine,
        backend,
        entities=entities,
        ordinal=1,
        target_plan="pro",
        effective_at=ORIGIN + timedelta(days=5),
    )

    with pytest.raises(RuntimeError, match="pending plan change"):
        request_plan_change(
            persistence,
            engine,
            backend,
            entities=entities,
            ordinal=2,
            target_plan="enterprise",
            effective_at=ORIGIN + timedelta(days=10),
        )


def test_plan_change_must_be_future_and_inside_current_term():
    persistence, entities, engine, backend = _runtime()

    with pytest.raises(ValueError, match="inside the active term"):
        request_plan_change(
            persistence,
            engine,
            backend,
            entities=entities,
            ordinal=1,
            target_plan="pro",
            effective_at=ORIGIN,
        )

    with pytest.raises(ValueError, match="inside the active term"):
        request_plan_change(
            persistence,
            engine,
            backend,
            entities=entities,
            ordinal=2,
            target_plan="pro",
            effective_at=ORIGIN + timedelta(days=30),
        )


def test_cancellation_at_period_end_cancels_pending_amendment():
    persistence, entities, engine, backend = _runtime()
    change = request_plan_change(
        persistence,
        engine,
        backend,
        entities=entities,
        ordinal=1,
        target_plan="pro",
        effective_at=ORIGIN + timedelta(days=5),
    )

    term_end = request_cancellation(
        persistence,
        engine,
        entities=entities,
    )
    persisted_change = persistence.entity("saas_change_request", change.id)
    subscription = persistence.entity("saas_subscription", entities.subscription_id)

    assert persisted_change is not None and persisted_change.state == "cancelled"
    assert subscription is not None and subscription.state == "cancellation_pending"
    assert len(persistence.scheduled_work()) == 2

    backend.run_until(term_end)
    subscription = persistence.entity("saas_subscription", entities.subscription_id)
    entitlement = persistence.entity(
        "saas_entitlement",
        subscription.attributes["active_entitlement_id"],
    )
    assert subscription is not None and subscription.state == "ended"
    assert entitlement is not None and entitlement.state == "revoked"
    assert persistence.scheduled_work() == ()


def test_withdraw_cancellation_removes_end_boundaries():
    persistence, entities, engine, _ = _runtime()
    request_cancellation(persistence, engine, entities=entities)

    subscription = withdraw_cancellation(
        persistence,
        engine,
        entities=entities,
    )

    assert subscription.state == "active"
    assert persistence.scheduled_work() == ()


def test_no_new_plan_change_after_cancellation_request():
    persistence, entities, engine, backend = _runtime()
    request_cancellation(persistence, engine, entities=entities)

    with pytest.raises(RuntimeError, match="active subscription"):
        request_plan_change(
            persistence,
            engine,
            backend,
            entities=entities,
            ordinal=1,
            target_plan="pro",
            effective_at=ORIGIN + timedelta(days=5),
        )

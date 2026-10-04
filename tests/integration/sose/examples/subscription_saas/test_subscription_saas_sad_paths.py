from datetime import timedelta

import pytest

from sose.backends.simpy import SimPyBackend
from sose.examples.subscription_saas.scenarios import ORIGIN
from sose.examples.subscription_saas import simulation as saas_simulation
from sose.examples.subscription_saas.simulation import (
    build_runtime,
    request_cancellation,
    request_plan_change,
    run_happy_path,
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


def test_plan_change_rejects_target_equal_to_active_plan():
    persistence, entities, engine, backend = _runtime()
    subscription = persistence.entity("saas_subscription", entities.subscription_id)
    assert subscription is not None
    active_plan = str(subscription.attributes["plan_code"])

    with pytest.raises(ValueError, match="target plan must differ"):
        request_plan_change(
            persistence,
            engine,
            backend,
            entities=entities,
            ordinal=3,
            target_plan=active_plan,
            effective_at=ORIGIN + timedelta(days=5),
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


def test_plan_change_ordinal_must_be_positive():
    persistence, entities, engine, backend = _runtime()
    with pytest.raises(ValueError, match="ordinal must be positive"):
        request_plan_change(
            persistence,
            engine,
            backend,
            entities=entities,
            ordinal=0,
            target_plan="pro",
            effective_at=ORIGIN + timedelta(days=5),
        )


def test_existing_change_identity_is_idempotent_and_rejects_drift():
    persistence, entities, engine, backend = _runtime()
    original = request_plan_change(
        persistence,
        engine,
        backend,
        entities=entities,
        ordinal=1,
        target_plan="pro",
        effective_at=ORIGIN + timedelta(days=5),
    )
    replay = request_plan_change(
        persistence,
        engine,
        backend,
        entities=entities,
        ordinal=1,
        target_plan="pro",
        effective_at=ORIGIN + timedelta(days=5),
    )
    assert replay.id == original.id

    with pytest.raises(ValueError, match="identity already exists with different intent"):
        request_plan_change(
            persistence,
            engine,
            backend,
            entities=entities,
            ordinal=1,
            target_plan="enterprise",
            effective_at=ORIGIN + timedelta(days=5),
        )


def test_reconcile_plan_change_state_guards_and_missing_entity_error():
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
    assert saas_simulation.reconcile_plan_change(
        persistence,
        engine,
        entities=entities,
        change_id=change.id,
    ) is None

    cancelled = persistence.entity("saas_change_request", change.id)
    assert cancelled is not None
    cancelled.state = "cancelled"
    with persistence.transaction() as uow:
        uow.save_entity(cancelled)
    with pytest.raises(RuntimeError, match="only an applied change"):
        saas_simulation.reconcile_plan_change(
            persistence,
            engine,
            entities=entities,
            change_id=change.id,
        )

    with pytest.raises(RuntimeError, match="saas_change_request was not persisted"):
        saas_simulation._entity(persistence, "saas_change_request", "missing")


def test_request_and_withdraw_cancellation_terminal_and_guard_paths():
    persistence, entities, engine, _ = _runtime()
    subscription = persistence.entity("saas_subscription", entities.subscription_id)
    assert subscription is not None

    with pytest.raises(RuntimeError, match="withdrawal requires pending cancellation"):
        withdraw_cancellation(persistence, engine, entities=entities)

    subscription.state = "ended"
    with persistence.transaction() as uow:
        uow.save_entity(subscription)
    term_end = request_cancellation(persistence, engine, entities=entities)
    assert term_end.isoformat() == subscription.attributes["term_end_at"]

    subscription.state = "draft"
    with persistence.transaction() as uow:
        uow.save_entity(subscription)
    with pytest.raises(RuntimeError, match="cancellation requires active subscription"):
        request_cancellation(persistence, engine, entities=entities)


def test_request_cancellation_skips_non_scheduled_change_and_keeps_single_boundaries():
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
    staged = persistence.entity("saas_change_request", change.id)
    assert staged is not None
    staged.state = "applied"
    with persistence.transaction() as uow:
        uow.save_entity(staged)

    request_cancellation(persistence, engine, entities=entities)
    first_len = len(persistence.scheduled_work())
    request_cancellation(persistence, engine, entities=entities)
    assert len(persistence.scheduled_work()) == first_len


def test_run_happy_path_raises_when_plan_change_not_reconciled(monkeypatch):
    monkeypatch.setattr(saas_simulation, "reconcile_plan_change", lambda *args, **kwargs: None)
    with pytest.raises(RuntimeError, match="did not become applicable"):
        run_happy_path()

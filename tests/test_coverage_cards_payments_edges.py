from __future__ import annotations

from datetime import timedelta
from types import SimpleNamespace

import pytest

from sose.backends.simpy import SimPyBackend
from sose.examples.cards_payments import simulation as cards_simulation
from sose.examples.cards_payments.scenarios import ORIGIN
from sose.examples.cards_payments.simulation import (
    _payment,
    build_runtime,
    ensure_dispute,
    reconcile_authorization,
    reconcile_capture_and_schedule_settlement,
    reconcile_dispute,
    reconcile_refund,
    reconcile_reversal,
    reconcile_settlement,
    run_dispute_path,
    run_refund_path,
    run_retry_path,
    seed_reference,
    start_dispute,
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


def _set_payment_state(persistence, entities, state):
    payment = persistence.entity("card_payment", entities.payment_id)
    assert payment is not None
    payment.state = state
    _save(persistence, payment)
    return payment


def test_missing_payment_guard_is_observable():
    persistence = MemoryPersistence()
    entities = type("Entities", (), {"payment_id": "missing"})()

    with pytest.raises(RuntimeError, match="payment was not persisted"):
        _payment(persistence, entities)


def test_authorization_rejects_unknown_outcome():
    persistence, entities, engine, backend = _runtime()

    with pytest.raises(ValueError, match="unsupported authorization outcome"):
        reconcile_authorization(
            persistence,
            engine,
            backend,
            entities=entities,
            outcome="maybe",
        )


@pytest.mark.parametrize("state", ["authorized", "declined"])
def test_authorization_is_idempotent_for_terminal_decision_states(state):
    persistence, entities, engine, backend = _runtime()
    _set_payment_state(persistence, entities, state)

    assert reconcile_authorization(
        persistence,
        engine,
        backend,
        entities=entities,
    ) is True


def test_authorization_returns_false_for_unrelated_state():
    persistence, entities, engine, backend = _runtime()
    _set_payment_state(persistence, entities, "captured")

    assert reconcile_authorization(
        persistence,
        engine,
        backend,
        entities=entities,
    ) is False


def test_authorization_waits_for_processor_capacity():
    persistence, entities, engine, backend = _runtime()
    blocker = engine.resources.ensure_requested(
        backend,
        resource_name="authorization_processor",
        request_id="authorization:blocker",
        requested_at=backend.now,
    )
    assert blocker is not None

    assert reconcile_authorization(
        persistence,
        engine,
        backend,
        entities=entities,
    ) is False
    assert any(
        demand.request_id == f"authorization-processor:{entities.payment_id}"
        for demand in persistence.resource_demands()
    )


def test_reversal_is_idempotent_and_rejects_non_authorized_payment():
    persistence, entities, engine, _ = _runtime()
    _set_payment_state(persistence, entities, "reversed")
    assert reconcile_reversal(
        persistence,
        engine,
        entities=entities,
    ) is True

    _set_payment_state(persistence, entities, "declined")
    assert reconcile_reversal(
        persistence,
        engine,
        entities=entities,
    ) is False


def test_capture_requires_authorized_payment():
    persistence, entities, engine, backend = _runtime()

    with pytest.raises(RuntimeError, match="payment is not capturable"):
        reconcile_capture_and_schedule_settlement(
            persistence,
            engine,
            backend,
            entities=entities,
        )


def test_settlement_rejects_unknown_outcome():
    persistence, entities, engine, backend = _runtime()

    with pytest.raises(ValueError, match="unsupported settlement outcome"):
        reconcile_settlement(
            persistence,
            engine,
            backend,
            entities=entities,
            outcome="unknown",
        )


def test_settlement_is_noop_for_settled_and_non_pending_states():
    persistence, entities, engine, backend = _runtime()
    _set_payment_state(persistence, entities, "settled")
    assert reconcile_settlement(
        persistence,
        engine,
        backend,
        entities=entities,
    ) is None

    _set_payment_state(persistence, entities, "authorized")
    assert reconcile_settlement(
        persistence,
        engine,
        backend,
        entities=entities,
    ) is None


def test_settlement_respects_processor_outage(monkeypatch):
    persistence, entities, engine, backend = _runtime()
    _set_payment_state(persistence, entities, "settlement_pending")
    monkeypatch.setattr(
        engine.context.scenarios,
        "attribute",
        lambda name, default=True: False
        if name == "payments.processor.available"
        else default,
    )

    assert reconcile_settlement(
        persistence,
        engine,
        backend,
        entities=entities,
    ) is None
    payment = persistence.entity("card_payment", entities.payment_id)
    assert payment is not None and payment.state == "settlement_pending"
    assert persistence.resource_demands() == ()


def test_settlement_waits_for_processor_capacity():
    persistence, entities, engine, backend = _runtime()
    _set_payment_state(persistence, entities, "settlement_pending")
    blocker = engine.resources.ensure_requested(
        backend,
        resource_name="settlement_processor",
        request_id="settlement:blocker",
        requested_at=backend.now,
    )
    assert blocker is not None

    assert reconcile_settlement(
        persistence,
        engine,
        backend,
        entities=entities,
    ) is None
    assert any(
        demand.request_id == f"settlement-processor:{entities.payment_id}"
        for demand in persistence.resource_demands()
    )


def test_settlement_retry_uses_requested_delay():
    persistence, entities, engine, backend = _runtime()
    _set_payment_state(persistence, entities, "settlement_pending")

    due_at = reconcile_settlement(
        persistence,
        engine,
        backend,
        entities=entities,
        outcome="retry",
        retry_after=timedelta(minutes=37),
    )

    assert due_at == backend.now + timedelta(minutes=37)
    pending = engine.scheduler.find_pending(
        entity_type="card_payment",
        entity_id=entities.payment_id,
        name="retry_settlement",
    )
    assert pending is not None and pending.work.due_at == due_at


def test_refund_is_idempotent_and_rejects_non_settled_payment():
    persistence, entities, engine, backend = _runtime()
    _set_payment_state(persistence, entities, "refunded")
    assert reconcile_refund(
        persistence,
        engine,
        backend,
        entities=entities,
    ) is True

    _set_payment_state(persistence, entities, "authorized")
    assert reconcile_refund(
        persistence,
        engine,
        backend,
        entities=entities,
    ) is False


def test_refund_respects_processor_outage(monkeypatch):
    persistence, entities, engine, backend = _runtime()
    _set_payment_state(persistence, entities, "settled")
    monkeypatch.setattr(
        engine.context.scenarios,
        "attribute",
        lambda name, default=True: False
        if name == "payments.processor.available"
        else default,
    )

    assert reconcile_refund(
        persistence,
        engine,
        backend,
        entities=entities,
    ) is False
    assert persistence.resource_demands() == ()


def test_refund_waits_for_processor_capacity():
    persistence, entities, engine, backend = _runtime()
    _set_payment_state(persistence, entities, "settled")
    blocker = engine.resources.ensure_requested(
        backend,
        resource_name="settlement_processor",
        request_id="refund:blocker",
        requested_at=backend.now,
    )
    assert blocker is not None

    assert reconcile_refund(
        persistence,
        engine,
        backend,
        entities=entities,
    ) is False
    assert any(
        demand.request_id == f"refund-processor:{entities.payment_id}"
        for demand in persistence.resource_demands()
    )


def test_dispute_requires_settled_payment_and_is_idempotent_once_created():
    persistence, entities, engine, _ = _runtime()

    with pytest.raises(RuntimeError, match="requires a settled payment"):
        ensure_dispute(persistence, engine, entities=entities)

    _set_payment_state(persistence, entities, "settled")
    first = ensure_dispute(persistence, engine, entities=entities)
    entities_before = tuple(persistence.entities())
    second = ensure_dispute(persistence, engine, entities=entities)

    assert second == first
    assert tuple(persistence.entities()) == entities_before


def test_start_dispute_requires_open_dispute():
    persistence, entities, engine, backend = _runtime()
    _set_payment_state(persistence, entities, "settled")
    dispute = ensure_dispute(persistence, engine, entities=entities)
    dispute.state = "evidence_requested"
    _save(persistence, dispute)

    with pytest.raises(RuntimeError, match="dispute is not open"):
        start_dispute(
            persistence,
            engine,
            backend,
            entities=entities,
        )


def test_dispute_reconciliation_rejects_unknown_outcome():
    persistence, entities, engine, backend = _runtime()

    with pytest.raises(ValueError, match="unsupported dispute outcome"):
        reconcile_dispute(
            persistence,
            engine,
            backend,
            outcome="unknown",
        )


def test_dispute_reconciliation_requires_existing_under_review_dispute():
    persistence, entities, engine, backend = _runtime()

    assert reconcile_dispute(
        persistence,
        engine,
        backend,
    ) is False

    _set_payment_state(persistence, entities, "settled")
    ensure_dispute(persistence, engine, entities=entities)
    assert reconcile_dispute(
        persistence,
        engine,
        backend,
    ) is False


def test_dispute_reconciliation_waits_for_analyst_capacity():
    persistence, entities, engine, backend = _runtime()
    _set_payment_state(persistence, entities, "settled")
    dispute = ensure_dispute(persistence, engine, entities=entities)
    dispute.state = "under_review"
    _save(persistence, dispute)

    blocker = engine.resources.ensure_requested(
        backend,
        resource_name="dispute_analyst",
        request_id="dispute:blocker",
        requested_at=backend.now,
    )
    assert blocker is not None

    assert reconcile_dispute(
        persistence,
        engine,
        backend,
    ) is False
    assert any(
        demand.request_id == f"dispute-analyst:{dispute.id}"
        for demand in persistence.resource_demands()
    )


def test_start_dispute_raises_when_dispute_disappears_after_evidence_request(monkeypatch):
    dispute = SimpleNamespace(id="D1", state="opened")
    monkeypatch.setattr(cards_simulation, "ensure_dispute", lambda *args, **kwargs: dispute)
    monkeypatch.setattr(cards_simulation, "_dispatch", lambda *args, **kwargs: None)
    monkeypatch.setattr(cards_simulation, "_dispute", lambda persistence: None)

    with pytest.raises(RuntimeError, match="dispute disappeared"):
        start_dispute(
            object(),
            object(),
            SimpleNamespace(now=ORIGIN),
            entities=SimpleNamespace(),
        )


def test_reconcile_dispute_raises_when_dispute_disappears_before_chargeback(monkeypatch):
    dispute = SimpleNamespace(id="D2", state="under_review")
    states = iter([dispute, None])
    monkeypatch.setattr(cards_simulation, "_dispute", lambda persistence: next(states))

    engine = SimpleNamespace(
        resources=SimpleNamespace(
            ensure_requested=lambda *args, **kwargs: object(),
            withdraw=lambda *args, **kwargs: None,
        )
    )

    with pytest.raises(RuntimeError, match="dispute disappeared"):
        reconcile_dispute(
            object(),
            engine,
            SimpleNamespace(now=ORIGIN),
        )


def test_reconcile_dispute_raises_when_dispute_disappears_before_resolution(monkeypatch):
    first = SimpleNamespace(id="D3", state="under_review")
    second = SimpleNamespace(id="D3", state="under_review")
    states = iter([first, second, None])
    monkeypatch.setattr(cards_simulation, "_dispute", lambda persistence: next(states))
    monkeypatch.setattr(cards_simulation, "_dispatch", lambda *args, **kwargs: None)

    engine = SimpleNamespace(
        resources=SimpleNamespace(
            ensure_requested=lambda *args, **kwargs: object(),
            withdraw=lambda *args, **kwargs: None,
        )
    )

    with pytest.raises(RuntimeError, match="dispute disappeared"):
        reconcile_dispute(
            object(),
            engine,
            SimpleNamespace(now=ORIGIN),
        )


def test_prepare_authorized_raises_when_authorization_capacity_is_unavailable(monkeypatch):
    engine = SimpleNamespace(rebuild_backend=lambda backend: None)
    monkeypatch.setattr(cards_simulation, "seed_reference", lambda persistence: SimpleNamespace())
    monkeypatch.setattr(cards_simulation, "build_runtime", lambda persistence: (None, engine))
    monkeypatch.setattr(cards_simulation, "reconcile_authorization", lambda *args, **kwargs: False)

    with pytest.raises(RuntimeError, match="authorization capacity unavailable"):
        cards_simulation._prepare_authorized(MemoryPersistence())


def test_run_retry_path_raises_when_retry_is_not_scheduled(monkeypatch):
    backend = SimpleNamespace(
        now=ORIGIN,
        run_until=lambda due_at: None,
    )
    monkeypatch.setattr(
        cards_simulation,
        "_prepare_authorized",
        lambda persistence: (SimpleNamespace(), object(), backend),
    )
    monkeypatch.setattr(
        cards_simulation,
        "reconcile_capture_and_schedule_settlement",
        lambda *args, **kwargs: ORIGIN,
    )
    monkeypatch.setattr(cards_simulation, "reconcile_settlement", lambda *args, **kwargs: None)

    with pytest.raises(RuntimeError, match="settlement retry was not scheduled"):
        run_retry_path()


def test_run_refund_path_raises_when_refund_capacity_is_unavailable(monkeypatch):
    monkeypatch.setattr(
        cards_simulation,
        "run_happy_path",
        lambda: (MemoryPersistence(), SimpleNamespace()),
    )
    monkeypatch.setattr(
        cards_simulation,
        "build_runtime",
        lambda persistence, now, tick: (None, SimpleNamespace(rebuild_backend=lambda backend: None)),
    )
    monkeypatch.setattr(cards_simulation, "reconcile_refund", lambda *args, **kwargs: False)

    with pytest.raises(RuntimeError, match="refund capacity unavailable"):
        run_refund_path()


def test_run_dispute_path_raises_when_dispute_analyst_is_unavailable(monkeypatch):
    monkeypatch.setattr(
        cards_simulation,
        "run_happy_path",
        lambda: (MemoryPersistence(), SimpleNamespace()),
    )
    monkeypatch.setattr(
        cards_simulation,
        "build_runtime",
        lambda persistence, now, tick: (None, SimpleNamespace(rebuild_backend=lambda backend: None)),
    )
    monkeypatch.setattr(cards_simulation, "start_dispute", lambda *args, **kwargs: ORIGIN)
    monkeypatch.setattr(cards_simulation, "reconcile_dispute", lambda *args, **kwargs: False)

    with pytest.raises(RuntimeError, match="dispute analyst unavailable"):
        run_dispute_path()

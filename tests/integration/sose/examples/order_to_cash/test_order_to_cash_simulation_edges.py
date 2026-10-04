from __future__ import annotations

from types import SimpleNamespace

import pytest

from sose.backends.simpy import SimPyBackend
from sose.examples.order_to_cash import simulation as o2c_simulation
from sose.examples.order_to_cash.simulation import (
    ORIGIN,
    build_runtime,
    ensure_collection_case,
    reconcile_collection,
    reconcile_credit,
    reconcile_fulfillment,
    seed_reference,
)
from sose.persistence.memory import MemoryPersistence


def _runtime():
    persistence = MemoryPersistence()
    entities = seed_reference(persistence)
    _, engine = build_runtime(persistence)
    backend = SimPyBackend(origin=ORIGIN)
    engine.rebuild_backend(backend)
    return persistence, entities, engine, backend


def _save(persistence: MemoryPersistence, entity) -> None:
    with persistence.transaction() as uow:
        uow.save_entity(entity)


def test_reconcile_credit_returns_false_for_nonrecoverable_order_state():
    persistence, entities, engine, _ = _runtime()
    order = persistence.entity("sales_order", entities.order_id)
    assert order is not None
    order.state = "cancelled"
    _save(persistence, order)

    assert (
        reconcile_credit(
            persistence,
            engine,
            entities=entities,
        )
        is False
    )


def test_reconcile_fulfillment_terminal_and_invalid_state_guards_are_explicit():
    persistence, entities, engine, backend = _runtime()
    order = persistence.entity("sales_order", entities.order_id)
    assert order is not None
    order.state = "fulfilled"
    _save(persistence, order)

    assert reconcile_fulfillment(
        persistence,
        engine,
        backend,
        entities=entities,
    )

    order.state = "submitted"
    _save(persistence, order)
    assert (
        reconcile_fulfillment(
            persistence,
            engine,
            backend,
            entities=entities,
        )
        is False
    )


def test_reconcile_fulfillment_skips_fulfill_dispatch_when_order_state_already_terminal(monkeypatch):
    persistence, entities, engine, backend = _runtime()

    states = iter(
        [
            SimpleNamespace(id=entities.order_id, state="ordered", version=1),
            SimpleNamespace(id=entities.order_id, state="fulfilled", version=2),
            SimpleNamespace(id=entities.order_id, state="fulfilled", version=2),
        ]
    )
    seen_states: list[str] = []

    def _order(*args, **kwargs):
        value = next(states)
        seen_states.append(value.state)
        return value

    monkeypatch.setattr(o2c_simulation, "_order", _order)
    events: list[str] = []
    monkeypatch.setattr(
        o2c_simulation,
        "_dispatch",
        lambda _engine, _entity, event, **kwargs: events.append(event),
    )

    assert reconcile_fulfillment(
        persistence,
        engine,
        backend,
        entities=entities,
    )
    assert seen_states[:2] == ["ordered", "fulfilled"]
    assert events == []


def test_ensure_collection_case_raises_when_receivable_is_not_persisted(monkeypatch):
    persistence, entities, engine, _ = _runtime()
    monkeypatch.setattr(o2c_simulation, "_receivable", lambda *args, **kwargs: None)

    with pytest.raises(RuntimeError, match="receivable was not persisted"):
        ensure_collection_case(
            persistence,
            engine,
            entities=entities,
        )


def test_reconcile_collection_raises_when_collection_case_disappears(monkeypatch):
    persistence, entities, engine, backend = _runtime()
    fake_case = SimpleNamespace(
        id="CC-1",
        state="opened",
        attributes={"receivable_id": "R-1"},
    )
    monkeypatch.setattr(o2c_simulation, "ensure_collection_case", lambda *args, **kwargs: fake_case)
    monkeypatch.setattr(o2c_simulation, "_collection_case", lambda *args, **kwargs: None)

    with pytest.raises(RuntimeError, match="collection case disappeared"):
        reconcile_collection(
            persistence,
            engine,
            backend,
            entities=entities,
        )


def test_reconcile_collection_skips_assignment_when_case_is_already_assigned(monkeypatch):
    persistence, entities, engine, backend = _runtime()
    case = SimpleNamespace(
        id="CC-2",
        state="assigned",
        attributes={"receivable_id": "R-2"},
    )
    monkeypatch.setattr(o2c_simulation, "ensure_collection_case", lambda *args, **kwargs: case)
    monkeypatch.setattr(o2c_simulation, "_collection_case", lambda *args, **kwargs: case)
    events: list[str] = []
    monkeypatch.setattr(
        o2c_simulation,
        "_dispatch",
        lambda _engine, _entity, event, **kwargs: events.append(event),
    )

    assert reconcile_collection(
        persistence,
        engine,
        backend,
        entities=entities,
    )
    assert events == ["contact"]


def test_reconcile_collection_skips_contact_when_assigned_transition_does_not_materialize(
    monkeypatch,
):
    persistence, entities, engine, backend = _runtime()
    case = SimpleNamespace(
        id="CC-3",
        state="opened",
        attributes={"receivable_id": "R-3"},
    )
    monkeypatch.setattr(o2c_simulation, "ensure_collection_case", lambda *args, **kwargs: case)
    monkeypatch.setattr(o2c_simulation, "_collection_case", lambda *args, **kwargs: case)
    events: list[str] = []
    monkeypatch.setattr(
        o2c_simulation,
        "_dispatch",
        lambda _engine, _entity, event, **kwargs: events.append(event),
    )

    assert reconcile_collection(
        persistence,
        engine,
        backend,
        entities=entities,
        promise=True,
    )
    assert events == ["assign"]

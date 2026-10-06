from __future__ import annotations

from sose.backends.simpy import SimPyBackend
from sose.examples.order_to_cash.simulation import (
    ORIGIN,
    build_runtime,
    collect_receivable,
    ensure_collection_case,
    reconcile_collection,
    reconcile_credit,
    reconcile_fulfillment,
    schedule_due,
    schedule_overdue,
    seed_reference,
    ship_invoice_and_ensure_receivable,
)
from sose.persistence.memory import MemoryPersistence
from sose.testing.restart import restart_reference_runtime


def _prepare_promised_collection(store: MemoryPersistence):
    entities = seed_reference(store)
    _, engine = build_runtime(store)
    backend = SimPyBackend(origin=ORIGIN)
    engine.rebuild_backend(backend)

    assert reconcile_credit(store, engine, entities=entities)
    assert reconcile_fulfillment(store, engine, backend, entities=entities)
    receivable = ship_invoice_and_ensure_receivable(store, engine, entities=entities)

    due_at = schedule_due(store, engine, backend, entities=entities)
    backend.run_until(due_at)
    overdue_at = schedule_overdue(store, engine, backend, entities=entities)
    backend.run_until(overdue_at)

    case = ensure_collection_case(store, engine, entities=entities)
    assert reconcile_collection(
        store,
        engine,
        backend,
        entities=entities,
        promise=True,
    )
    case = store.entity("collection_case", case.id)
    assert case is not None and case.state == "promised"
    assert len(store.scheduled_work()) == 1
    followup_at = store.scheduled_work()[0].due_at
    return entities, receivable.id, case.id, engine, backend, followup_at


def _finish_collection(store, entities, engine, backend, followup_at) -> None:
    backend.run_until(followup_at)
    assert collect_receivable(store, engine, entities=entities)


def _snapshot(store, entities, receivable_id, case_id):
    return {
        "order": store.entity("sales_order", entities.order_id),
        "receivable": store.entity("receivable", receivable_id),
        "collection_case": store.entity("collection_case", case_id),
        "events": store.events(),
        "scheduled_work": store.scheduled_work(),
        "position": store.simulation_position(),
        "scenario": store.scenario_state(),
        "resource_definitions": store.resource_definitions(),
        "resource_demands": store.resource_demands(),
        "resource_reservations": store.resource_reservations(),
        "resource_release_intents": store.resource_release_intents(),
    }


def _run_continuous():
    store = MemoryPersistence()
    entities, receivable_id, case_id, engine, backend, followup_at = (
        _prepare_promised_collection(store)
    )
    _finish_collection(store, entities, engine, backend, followup_at)
    return store, entities, receivable_id, case_id, engine, backend


def _run_restarted():
    store = MemoryPersistence()
    entities, receivable_id, case_id, _, backend_before, followup_at = (
        _prepare_promised_collection(store)
    )
    rebuilt = restart_reference_runtime(
        store,
        build_runtime,
        backend_before,
        backend_factory=SimPyBackend,
    )
    _finish_collection(
        store,
        entities,
        rebuilt.engine,
        rebuilt.backend,
        followup_at,
    )
    return store, entities, receivable_id, case_id, rebuilt.engine, rebuilt.backend


def test_order_to_cash_collection_path_is_fully_restart_equivalent() -> None:
    continuous = _run_continuous()
    restarted = _run_restarted()

    c_store, c_entities, c_receivable_id, c_case_id, _, _ = continuous
    r_store, r_entities, r_receivable_id, r_case_id, _, _ = restarted

    assert _snapshot(r_store, r_entities, r_receivable_id, r_case_id) == _snapshot(
        c_store,
        c_entities,
        c_receivable_id,
        c_case_id,
    )

    assert r_store.entity("sales_order", r_entities.order_id).state == "invoiced"
    assert r_store.entity("receivable", r_receivable_id).state == "collected"
    assert r_store.entity("collection_case", r_case_id).state == "resolved"
    assert r_store.scheduled_work() == ()
    assert r_store.resource_demands() == ()
    assert r_store.resource_reservations() == ()
    assert r_store.resource_release_intents() == ()


def test_terminal_order_to_cash_business_replay_is_idempotent() -> None:
    store, entities, receivable_id, case_id, engine, backend = _run_continuous()
    before = _snapshot(store, entities, receivable_id, case_id)

    receivable = ship_invoice_and_ensure_receivable(store, engine, entities=entities)
    case = ensure_collection_case(store, engine, entities=entities)
    assert receivable.id == receivable_id
    assert case.id == case_id
    assert schedule_due(store, engine, backend, entities=entities) == backend.now
    assert collect_receivable(store, engine, entities=entities)

    assert _snapshot(store, entities, receivable_id, case_id) == before

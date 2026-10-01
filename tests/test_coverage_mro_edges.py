from __future__ import annotations

from types import SimpleNamespace

import pytest

from sose.backends.simpy import SimPyBackend
from sose.examples.mro import simulation as mro
from sose.persistence.memory import MemoryPersistence


def _released_runtime(*, quantity: float = 1.0):
    persistence = MemoryPersistence()
    entities = mro.seed_reference(persistence, quantity=quantity)
    _, engine = mro.build_runtime(persistence)
    backend = SimPyBackend(origin=mro.ORIGIN)
    engine.rebuild_backend(backend)
    backend.run_until(mro.ORIGIN.replace(hour=9))
    return persistence, entities, engine, backend


def _hide_entity(monkeypatch, persistence, entity_type: str, entity_id: str):
    original = persistence.entity

    def hidden(current_type: str, current_id: str):
        if current_type == entity_type and current_id == entity_id:
            return None
        return original(current_type, current_id)

    monkeypatch.setattr(persistence, "entity", hidden)


@pytest.mark.parametrize("quantity", [0.0, -1.0, mro.PART_CAPACITY + 1.0])
def test_seed_reference_rejects_quantity_outside_spare_part_capacity(quantity):
    with pytest.raises(ValueError, match="quantity must fit spare-parts capacity"):
        mro.seed_reference(MemoryPersistence(), quantity=quantity)


@pytest.mark.parametrize("quantity", [0.0, -1.0, mro.PART_CAPACITY + 1.0])
def test_seed_spare_parts_rejects_quantity_outside_capacity(quantity):
    persistence, _, engine, backend = _released_runtime()
    with pytest.raises(ValueError, match="quantity must fit spare-parts capacity"):
        mro.seed_spare_parts(engine, backend, quantity=quantity)


@pytest.mark.parametrize(
    ("function_name", "missing_type", "error"),
    [
        ("reconcile_material_availability", "work_order", "MRO entities were not persisted"),
        ("reconcile_part_issue", "part_demand", "MRO entities were not persisted"),
        ("reconcile_start", "work_order", "work order was not persisted"),
        ("reconcile_scenario_emergency", "work_order", "work order was not persisted"),
        ("reconcile_emergency_interrupt", "work_order", "work order was not persisted"),
        ("reconcile_emergency_resume", "work_order", "work order was not persisted"),
        ("reconcile_complete", "work_order", "work order was not persisted"),
        ("reconcile_cancel", "part_demand", "MRO entities were not persisted"),
    ],
)
def test_mro_reconcilers_fail_loudly_when_durable_entities_disappear(
    monkeypatch, function_name, missing_type, error
):
    persistence, entities, engine, backend = _released_runtime()
    entity_id = (
        entities.work_order_id
        if missing_type == "work_order"
        else entities.part_demand_id
    )
    _hide_entity(monkeypatch, persistence, missing_type, entity_id)
    function = getattr(mro, function_name)

    kwargs = {"entities": entities}
    if function_name in {
        "reconcile_material_availability",
        "reconcile_part_issue",
        "reconcile_start",
    }:
        kwargs["quantity"] = 1.0

    with pytest.raises(RuntimeError, match=error):
        if function_name == "reconcile_complete":
            function(persistence, engine, **kwargs)
        else:
            function(persistence, engine, backend, **kwargs)


def test_terminal_work_discards_late_regular_and_preemptive_grants(monkeypatch):
    persistence, entities, engine, backend = _released_runtime()
    mro.reconcile_cancel(persistence, engine, backend, entities=entities)

    regular = []
    preemptive = []
    monkeypatch.setattr(
        engine.resources,
        "withdraw",
        lambda _backend, request_id: regular.append(request_id),
    )
    monkeypatch.setattr(
        engine.preemptive_resources,
        "withdraw",
        lambda _backend, request_id: preemptive.append(request_id),
    )

    mro._discard_granted_capacity_if_terminal(
        persistence,
        engine,
        backend,
        work_order_id=entities.work_order_id,
        reservation=SimpleNamespace(request_id="technician-late"),
        preemptive=False,
    )
    mro._discard_granted_capacity_if_terminal(
        persistence,
        engine,
        backend,
        work_order_id=entities.work_order_id,
        reservation=SimpleNamespace(request_id="bay-late"),
        preemptive=True,
    )

    assert regular == ["technician-late"]
    assert preemptive == ["bay-late"]


def test_part_issue_direct_shortage_moves_released_entities_to_waiting_states():
    persistence, entities, engine, backend = _released_runtime(quantity=2.0)

    assert (
        mro.reconcile_part_issue(
            persistence,
            engine,
            backend,
            entities=entities,
            quantity=2.0,
        )
        is False
    )

    assert persistence.entity("work_order", entities.work_order_id).state == "waiting_material"
    assert persistence.entity("part_demand", entities.part_demand_id).state == "waiting_inventory"


def test_part_issue_returns_pending_when_backend_has_not_completed_withdrawals(monkeypatch):
    persistence, entities, engine, backend = _released_runtime()
    mro.seed_spare_parts(engine, backend, quantity=1.0)

    monkeypatch.setattr(backend, "run_until", lambda _at: 0)

    assert (
        mro.reconcile_part_issue(
            persistence,
            engine,
            backend,
            entities=entities,
            quantity=1.0,
        )
        is False
    )
    assert any(
        request.request_id == "consume-part-lot-1"
        for request in persistence.store_get_requests()
    )
    assert any(
        intent.request_id == "consume-spare-part-1"
        for intent in persistence.container_operation_intents()
    )


def test_reconcile_start_propagates_incomplete_part_issue(monkeypatch):
    persistence, entities, engine, backend = _released_runtime()

    monkeypatch.setattr(mro, "reconcile_material_availability", lambda *args, **kwargs: True)
    monkeypatch.setattr(mro, "reconcile_capacity", lambda *args, **kwargs: True)
    monkeypatch.setattr(mro, "reconcile_part_issue", lambda *args, **kwargs: False)

    assert (
        mro.reconcile_start(
            persistence,
            engine,
            backend,
            entities=entities,
            quantity=1.0,
        )
        is False
    )


def test_happy_path_reports_unavailable_prerequisites(monkeypatch):
    monkeypatch.setattr(mro, "reconcile_start", lambda *args, **kwargs: False)
    with pytest.raises(RuntimeError, match="MRO prerequisites are still unavailable"):
        mro.run_happy_path()

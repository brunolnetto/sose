from __future__ import annotations

import pytest

from sose.backends.simpy import SimPyBackend
from sose.examples.manufacturing import simulation as manufacturing
from sose.persistence.memory import MemoryPersistence


def _released_runtime(*, quantity: float = 5.0):
    persistence = MemoryPersistence()
    entities = manufacturing.seed_happy_path(persistence, quantity=quantity)
    _, engine = manufacturing.build_runtime(persistence)
    backend = SimPyBackend(origin=manufacturing.ORIGIN)
    engine.rebuild_backend(backend)
    backend.run_until(manufacturing.ORIGIN.replace(hour=9))
    return persistence, entities, engine, backend


def _producing_runtime(*, quantity: float = 5.0):
    persistence, entities, engine, backend = _released_runtime(quantity=quantity)
    manufacturing.seed_material(engine, backend, quantity=quantity)
    assert manufacturing.reconcile_setup_resources(
        persistence,
        engine,
        backend,
        entities=entities,
    )
    manufacturing.reconcile_material_issue(
        persistence,
        engine,
        backend,
        entities=entities,
        quantity=quantity,
    )
    return persistence, entities, engine, backend


def _inspection_runtime(*, quantity: float = 5.0):
    persistence, entities, engine, backend = _producing_runtime(quantity=quantity)
    manufacturing.reconcile_wip_output(
        persistence,
        engine,
        backend,
        entities=entities,
        quantity=quantity,
    )
    return persistence, entities, engine, backend


def _hide_entity(monkeypatch, persistence, entity_type: str, entity_id: str):
    original = persistence.entity

    def hidden(current_type: str, current_id: str):
        if current_type == entity_type and current_id == entity_id:
            return None
        return original(current_type, current_id)

    monkeypatch.setattr(persistence, "entity", hidden)


def test_setup_resources_fails_loudly_when_durable_entities_disappear(monkeypatch):
    persistence, entities, engine, backend = _released_runtime()
    _hide_entity(monkeypatch, persistence, "production_order", entities.production_order_id)

    with pytest.raises(RuntimeError, match="manufacturing entities were not persisted"):
        manufacturing.reconcile_setup_resources(
            persistence,
            engine,
            backend,
            entities=entities,
        )


def test_scenario_breakdown_is_noop_when_machine_down_flag_is_false():
    persistence, entities, engine, backend = _producing_runtime()

    assert manufacturing.reconcile_scenario_breakdown(
        persistence,
        engine,
        backend,
        entities=entities,
    ) is False


def test_breakdown_returns_pending_when_higher_priority_machine_holder_blocks_repair():
    persistence, entities, engine, backend = _released_runtime()

    engine.preemptive_resources.request(
        backend,
        resource_name="machine",
        request_id="critical-machine-holder",
        requested_at=backend.now,
        priority=0,
        preempt=False,
    )
    backend.run_until(backend.now)

    assert manufacturing.reconcile_breakdown(
        persistence,
        engine,
        backend,
        entities=entities,
    ) is False


def test_breakdown_without_displacement_is_not_committed_as_preemption():
    persistence, entities, engine, backend = _released_runtime()

    assert manufacturing.reconcile_breakdown(
        persistence,
        engine,
        backend,
        entities=entities,
    ) is False
    assert persistence.resource_preemption_results() == ()


def test_breakdown_fails_loudly_when_business_entities_disappear_after_preemption(monkeypatch):
    persistence, entities, engine, backend = _producing_runtime()
    _hide_entity(monkeypatch, persistence, "production_order", entities.production_order_id)

    with pytest.raises(RuntimeError, match="manufacturing entities were not persisted"):
        manufacturing.reconcile_breakdown(
            persistence,
            engine,
            backend,
            entities=entities,
        )


def test_repair_waits_when_machine_is_taken_after_repair_release():
    persistence, entities, engine, backend = _producing_runtime()
    assert manufacturing.reconcile_breakdown(
        persistence,
        engine,
        backend,
        entities=entities,
    )

    engine.preemptive_resources.request(
        backend,
        resource_name="machine",
        request_id="repair-successor-blocker",
        requested_at=backend.now,
        priority=0,
        preempt=False,
    )
    backend.run_until(backend.now)

    assert manufacturing.reconcile_repair(
        persistence,
        engine,
        backend,
        entities=entities,
    ) is False


def test_repair_fails_loudly_when_entities_disappear_after_machine_reacquire(monkeypatch):
    persistence, entities, engine, backend = _producing_runtime()
    assert manufacturing.reconcile_breakdown(
        persistence,
        engine,
        backend,
        entities=entities,
    )
    _hide_entity(monkeypatch, persistence, "production_order", entities.production_order_id)

    with pytest.raises(RuntimeError, match="manufacturing entities were not persisted"):
        manufacturing.reconcile_repair(
            persistence,
            engine,
            backend,
            entities=entities,
        )


def test_material_availability_requires_persisted_production_order(monkeypatch):
    persistence, entities, engine, _ = _released_runtime()
    _hide_entity(monkeypatch, persistence, "production_order", entities.production_order_id)

    with pytest.raises(RuntimeError, match="production order was not persisted"):
        manufacturing.reconcile_material_availability(
            persistence,
            engine,
            entities=entities,
        )


def test_material_issue_reports_pending_when_backend_has_not_committed(monkeypatch):
    persistence, entities, engine, backend = _released_runtime()
    manufacturing.seed_material(engine, backend, quantity=5.0)
    assert manufacturing.reconcile_setup_resources(
        persistence,
        engine,
        backend,
        entities=entities,
    )
    monkeypatch.setattr(backend, "run_until", lambda _at: 0)

    with pytest.raises(RuntimeError, match="raw material issue is still pending"):
        manufacturing.reconcile_material_issue(
            persistence,
            engine,
            backend,
            entities=entities,
            quantity=5.0,
        )


def test_material_issue_requires_persisted_order_after_durable_withdrawal(monkeypatch):
    persistence, entities, engine, backend = _released_runtime()
    manufacturing.seed_material(engine, backend, quantity=5.0)
    assert manufacturing.reconcile_setup_resources(
        persistence,
        engine,
        backend,
        entities=entities,
    )
    _hide_entity(monkeypatch, persistence, "production_order", entities.production_order_id)

    with pytest.raises(RuntimeError, match="production order was not persisted"):
        manufacturing.reconcile_material_issue(
            persistence,
            engine,
            backend,
            entities=entities,
            quantity=5.0,
        )


def test_wip_output_rejects_invalid_yield_factor(monkeypatch):
    persistence, entities, engine, backend = _producing_runtime()
    original_attribute = engine.context.scenarios.attribute

    def invalid_yield(key, default=None):
        if key == "manufacturing.yield.factor":
            return 0.0
        return original_attribute(key, default)

    monkeypatch.setattr(engine.context.scenarios, "attribute", invalid_yield)

    with pytest.raises(ValueError, match="yield factor must be in"):
        manufacturing.reconcile_wip_output(
            persistence,
            engine,
            backend,
            entities=entities,
            quantity=5.0,
        )


def test_wip_output_requires_durable_store_commit(monkeypatch):
    persistence, entities, engine, backend = _producing_runtime()
    monkeypatch.setattr(backend, "run_until", lambda _at: 0)

    with pytest.raises(RuntimeError, match="production WIP is not durably committed"):
        manufacturing.reconcile_wip_output(
            persistence,
            engine,
            backend,
            entities=entities,
            quantity=5.0,
        )


def test_wip_output_requires_persisted_manufacturing_entities(monkeypatch):
    persistence, entities, engine, backend = _producing_runtime()
    _hide_entity(monkeypatch, persistence, "production_order", entities.production_order_id)

    with pytest.raises(RuntimeError, match="manufacturing entities were not persisted"):
        manufacturing.reconcile_wip_output(
            persistence,
            engine,
            backend,
            entities=entities,
            quantity=5.0,
        )


def test_quality_pass_requires_persisted_order(monkeypatch):
    persistence, entities, engine, backend = _inspection_runtime()
    _hide_entity(monkeypatch, persistence, "production_order", entities.production_order_id)

    with pytest.raises(RuntimeError, match="production order was not persisted"):
        manufacturing.reconcile_quality_pass(
            persistence,
            engine,
            backend,
            entities=entities,
            quantity=5.0,
        )


def test_quality_pass_rejects_non_inspection_state():
    persistence, entities, engine, backend = _released_runtime()

    with pytest.raises(RuntimeError, match="production order is not ready for quality release"):
        manufacturing.reconcile_quality_pass(
            persistence,
            engine,
            backend,
            entities=entities,
            quantity=5.0,
        )


def test_quality_pass_requires_durable_wip():
    persistence, entities, engine, backend = _producing_runtime()
    order = persistence.entity("production_order", entities.production_order_id)
    assert order is not None and order.state == "producing"
    command = engine.context.commands.create(
        "begin_inspection",
        target=order,
        correlation_id=manufacturing.flow_correlation_id(),
        key=("coverage-manufacturing", order.id, "inspect-without-wip"),
    )
    engine.dispatch(command)
    assert persistence.entity("production_order", order.id).state == "inspection"

    with pytest.raises(RuntimeError, match="quality release requires durable WIP"):
        manufacturing.reconcile_quality_pass(
            persistence,
            engine,
            backend,
            entities=entities,
            quantity=5.0,
        )


def test_quality_pass_reports_pending_when_backend_has_not_committed_release(monkeypatch):
    persistence, entities, engine, backend = _inspection_runtime()
    monkeypatch.setattr(backend, "run_until", lambda _at: 0)

    with pytest.raises(RuntimeError, match="quality release is not durably committed"):
        manufacturing.reconcile_quality_pass(
            persistence,
            engine,
            backend,
            entities=entities,
            quantity=5.0,
        )


def test_quality_hold_requires_persisted_order(monkeypatch):
    persistence, entities, engine, _ = _inspection_runtime()
    _hide_entity(monkeypatch, persistence, "production_order", entities.production_order_id)

    with pytest.raises(RuntimeError, match="production order was not persisted"):
        manufacturing.reconcile_quality_hold(
            persistence,
            engine,
            entities=entities,
        )


def test_quality_hold_rejects_non_inspectable_state():
    persistence, entities, engine, _ = _released_runtime()

    with pytest.raises(RuntimeError, match="production order is not inspectable"):
        manufacturing.reconcile_quality_hold(
            persistence,
            engine,
            entities=entities,
        )


def test_rework_requires_persisted_order_and_operation(monkeypatch):
    persistence, entities, engine, _ = _released_runtime()
    _hide_entity(monkeypatch, persistence, "manufacturing_operation", entities.operation_id)

    with pytest.raises(RuntimeError, match="manufacturing entities were not persisted"):
        manufacturing.reconcile_rework(
            persistence,
            engine,
            entities=entities,
        )


def test_happy_path_fails_loudly_when_capacity_never_becomes_available(monkeypatch):
    monkeypatch.setattr(
        manufacturing,
        "reconcile_setup_resources",
        lambda *args, **kwargs: False,
    )

    with pytest.raises(RuntimeError, match="manufacturing capacity is still unavailable"):
        manufacturing.run_happy_path(quantity=5.0)

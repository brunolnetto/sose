from __future__ import annotations

from types import SimpleNamespace

import pytest

from sose.backends.simpy import SimPyBackend
from sose.persistence.memory import MemoryPersistence

from sose.examples.airports import definition as airports_definition
from sose.examples.credit_loans import definition as credit_definition
from sose.examples.field_service import definition as field_service_definition
from sose.examples.hospitals import definition as hospitals_definition
from sose.examples.insurance import definition as insurance_definition
from sose.examples.itsm import definition as itsm_definition
from sose.examples.itsm import simulation as itsm_simulation
from sose.examples.manufacturing import definition as manufacturing_definition
from sose.examples.order_to_cash import definition as order_to_cash_definition
from sose.examples.record_to_report import definition as r2r_definition
from sose.examples.subscription_saas import definition as saas_definition
from sose.examples.subscription_saas import simulation as saas_simulation
from sose.examples.telecom import definition as telecom_definition
from sose.examples.telecom import simulation as telecom_simulation
from sose.examples.warehouse_fulfillment import definition as warehouse_definition
from sose.examples.warehouse_fulfillment import simulation as warehouse_simulation


def _save(persistence: MemoryPersistence, entity) -> None:
    with persistence.transaction() as uow:
        uow.save_entity(entity)


@pytest.mark.parametrize(
    ("reconcile", "config", "entities", "message"),
    [
        (
            telecom_definition._reconcile_tick,
            telecom_definition.definition.default_config(),
            SimpleNamespace(product_order_id="missing"),
            "configured product order was not persisted",
        ),
        (
            warehouse_definition._reconcile_tick,
            warehouse_definition.definition.default_config(),
            SimpleNamespace(order_id="missing"),
            "configured fulfillment order was not persisted",
        ),
        (
            itsm_definition._reconcile_tick,
            itsm_definition.definition.default_config(),
            SimpleNamespace(incident_id="missing"),
            "configured incident was not persisted",
        ),
        (
            field_service_definition._reconcile_tick,
            field_service_definition.definition.default_config(),
            SimpleNamespace(work_order_id="missing"),
            "configured field work order was not persisted",
        ),
        (
            hospitals_definition._reconcile_tick,
            hospitals_definition.definition.default_config(),
            SimpleNamespace(admission_id="missing"),
            "configured admission was not persisted",
        ),
        (
            manufacturing_definition._reconcile_tick,
            manufacturing_definition.definition.default_config(),
            SimpleNamespace(production_order_id="missing"),
            "production order was not persisted",
        ),
        (
            insurance_definition._reconcile_tick,
            insurance_definition.definition.default_config(),
            SimpleNamespace(claim_id="missing"),
            "insurance claim was not persisted",
        ),
        (
            order_to_cash_definition._reconcile_tick,
            order_to_cash_definition.definition.default_config(),
            SimpleNamespace(order_id="missing"),
            "sales order was not persisted",
        ),
        (
            credit_definition._reconcile_tick,
            credit_definition.definition.default_config(),
            SimpleNamespace(application_id="missing"),
            "loan application was not persisted",
        ),
        (
            airports_definition._reconcile_tick,
            airports_definition.definition.default_config(),
            SimpleNamespace(turnaround_id="missing"),
            "configured turnaround was not persisted",
        ),
        (
            r2r_definition._reconcile_tick,
            r2r_definition.definition.default_config(),
            SimpleNamespace(
                journal_id="missing",
                reconciliation_id="missing",
                close_task_id="missing",
            ),
            "R2R reference entities were not persisted",
        ),
        (
            saas_definition._reconcile_tick,
            saas_definition.definition.default_config(),
            SimpleNamespace(subscription_id="missing"),
            "configured subscription was not persisted",
        ),
    ],
)
def test_definition_reconcile_guards_raise_for_missing_seeded_entities(
    reconcile,
    config,
    entities,
    message,
):
    persistence = MemoryPersistence()
    with pytest.raises(RuntimeError, match=message):
        reconcile(persistence, object(), object(), config, entities)


def test_telecom_reconcile_exits_when_order_already_completed(monkeypatch):
    persistence = MemoryPersistence()
    entities = telecom_simulation.seed_reference(persistence)
    _, engine = telecom_simulation.build_runtime(persistence)
    backend = SimPyBackend(origin=telecom_simulation.ORIGIN)
    engine.rebuild_backend(backend)

    order = persistence.entity("telecom_product_order", entities.product_order_id)
    assert order is not None
    order.state = "completed"
    _save(persistence, order)

    monkeypatch.setattr(
        telecom_definition,
        "schedule_activation",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("schedule_activation must not run")
        ),
    )
    monkeypatch.setattr(
        telecom_definition,
        "reconcile_activation",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("reconcile_activation must not run")
        ),
    )

    telecom_definition._reconcile_tick(
        persistence,
        engine,
        backend,
        telecom_definition.definition.default_config(),
        entities,
    )


def test_warehouse_reconcile_exits_when_order_already_shipped(monkeypatch):
    persistence = MemoryPersistence()
    entities = warehouse_simulation.seed_reference(persistence)
    _, engine = warehouse_simulation.build_runtime(persistence)
    backend = SimPyBackend(origin=warehouse_simulation.ORIGIN)
    engine.rebuild_backend(backend)

    order = persistence.entity("warehouse_fulfillment_order", entities.order_id)
    assert order is not None
    order.state = "shipped"
    _save(persistence, order)

    for name in ("allocate_order", "reconcile_fulfillment_services"):
        monkeypatch.setattr(
            warehouse_definition,
            name,
            lambda *args, _name=name, **kwargs: (_ for _ in ()).throw(
                AssertionError(f"{_name} must not run")
            ),
        )

    warehouse_definition._reconcile_tick(
        persistence,
        engine,
        backend,
        warehouse_definition.definition.default_config(),
        entities,
    )


def test_subscription_reconcile_exits_when_target_plan_is_already_active(monkeypatch):
    persistence = MemoryPersistence()
    entities = saas_simulation.seed_reference(persistence)
    _, engine = saas_simulation.build_runtime(persistence)
    backend = SimPyBackend(origin=saas_simulation.ORIGIN)
    engine.rebuild_backend(backend)

    config = saas_definition.definition.default_config().model_copy(
        update={"target_plan": "basic"}
    )

    monkeypatch.setattr(
        saas_definition,
        "request_plan_change",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("request_plan_change must not run")
        ),
    )
    monkeypatch.setattr(
        saas_definition,
        "reconcile_plan_change",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("reconcile_plan_change must not run")
        ),
    )

    saas_definition._reconcile_tick(
        persistence,
        engine,
        backend,
        config,
        entities,
    )


def test_itsm_reconcile_handles_resolved_incident_branch(monkeypatch):
    persistence = MemoryPersistence()
    entities = itsm_simulation.seed_reference(persistence)
    _, engine = itsm_simulation.build_runtime(persistence)
    backend = SimPyBackend(origin=itsm_simulation.ORIGIN)
    engine.rebuild_backend(backend)

    incident = persistence.entity("itsm_incident", entities.incident_id)
    assert incident is not None
    incident.state = "resolved"
    _save(persistence, incident)

    calls: list[tuple[tuple, dict]] = []

    def _resolve(*args, **kwargs):
        calls.append((args, kwargs))

    monkeypatch.setattr(itsm_definition, "resolve_incident", _resolve)

    itsm_definition._reconcile_tick(
        persistence,
        engine,
        backend,
        itsm_definition.definition.default_config(),
        entities,
    )

    assert len(calls) == 1
    assert calls[0][1]["close"] is True

from __future__ import annotations

from types import SimpleNamespace

from sose.backends.simpy import SimPyBackend
from sose.examples.mro import simulation as mro
from sose.persistence.memory import MemoryPersistence


def _released_runtime():
    persistence = MemoryPersistence()
    entities = mro.seed_reference(persistence, quantity=1.0)
    _, engine = mro.build_runtime(persistence)
    backend = SimPyBackend(origin=mro.ORIGIN)
    engine.rebuild_backend(backend)
    backend.run_until(mro.ORIGIN.replace(hour=9))
    return persistence, entities, engine, backend


def _active_work():
    persistence, entities, engine, backend = _released_runtime()
    mro.seed_spare_parts(engine, backend, quantity=1.0)
    assert mro.reconcile_start(
        persistence,
        engine,
        backend,
        entities=entities,
        quantity=1.0,
    )
    return persistence, entities, engine, backend


def test_active_emergency_helpers_preserve_pending_demand_identity():
    persistence, entities, engine, backend = _released_runtime()

    engine.preemptive_resources.request(
        backend,
        resource_name="maintenance_bay",
        request_id="bay-blocker",
        requested_at=backend.now,
        priority=0,
        preempt=False,
    )
    backend.run_until(backend.now)

    emergency_id = f"bay-emergency:{entities.work_order_id}:1"
    engine.preemptive_resources.request(
        backend,
        resource_name="maintenance_bay",
        request_id=emergency_id,
        requested_at=backend.now,
        priority=1,
        preempt=False,
    )
    backend.run_until(backend.now)

    assert any(
        demand.request_id == emergency_id
        for demand in persistence.preemptive_resource_demands()
    )
    assert (
        mro._active_emergency_request_id(persistence, entities.work_order_id)
        == emergency_id
    )
    assert (
        mro._next_emergency_request_id(persistence, entities.work_order_id)
        == emergency_id
    )


def test_interrupt_returns_false_when_normal_bay_vanished_without_emergency():
    persistence, entities, engine, backend = _active_work()
    normal_id = f"bay:{entities.work_order_id}"

    engine.preemptive_resources.withdraw(backend, normal_id)
    backend.run_until(backend.now)

    assert persistence.entity("work_order", entities.work_order_id).state == "in_progress"
    assert mro._active_emergency_request_id(persistence, entities.work_order_id) is None
    assert (
        mro.reconcile_emergency_interrupt(
            persistence,
            engine,
            backend,
            entities=entities,
        )
        is False
    )


def test_interrupt_waits_when_emergency_exists_without_committed_displacement():
    persistence, entities, engine, backend = _active_work()
    normal_id = f"bay:{entities.work_order_id}"
    emergency_id = f"bay-emergency:{entities.work_order_id}:1"

    engine.preemptive_resources.withdraw(backend, normal_id)
    backend.run_until(backend.now)
    engine.preemptive_resources.request(
        backend,
        resource_name="maintenance_bay",
        request_id=emergency_id,
        requested_at=backend.now,
        priority=1,
        preempt=False,
    )
    backend.run_until(backend.now)

    assert (
        mro._active_emergency_request_id(persistence, entities.work_order_id)
        == emergency_id
    )
    assert (
        mro._committed_emergency_result(persistence, entities.work_order_id)
        is None
    )
    assert (
        mro.reconcile_emergency_interrupt(
            persistence,
            engine,
            backend,
            entities=entities,
        )
        is False
    )


def test_interrupt_returns_pending_when_emergency_request_is_not_yet_granted(monkeypatch):
    persistence, entities, engine, backend = _active_work()

    monkeypatch.setattr(
        engine.preemptive_resources,
        "ensure_requested",
        lambda *args, **kwargs: None,
    )

    assert (
        mro.reconcile_emergency_interrupt(
            persistence,
            engine,
            backend,
            entities=entities,
        )
        is False
    )


def test_interrupt_with_grant_but_no_displacement_withdraws_emergency(monkeypatch):
    persistence, entities, engine, backend = _active_work()
    emergency_id = f"bay-emergency:{entities.work_order_id}:1"
    withdrawn: list[str] = []

    monkeypatch.setattr(
        mro,
        "_next_emergency_request_id",
        lambda *args, **kwargs: emergency_id,
    )
    monkeypatch.setattr(
        engine.preemptive_resources,
        "ensure_requested",
        lambda *args, **kwargs: SimpleNamespace(request_id=emergency_id),
    )
    monkeypatch.setattr(
        engine.preemptive_resources,
        "withdraw",
        lambda _backend, request_id: withdrawn.append(request_id),
    )

    assert (
        mro.reconcile_emergency_interrupt(
            persistence,
            engine,
            backend,
            entities=entities,
        )
        is False
    )
    assert withdrawn == [emergency_id]


def test_resume_is_idempotent_after_work_is_already_running():
    persistence, entities, engine, backend = _active_work()

    assert (
        mro.reconcile_emergency_resume(
            persistence,
            engine,
            backend,
            entities=entities,
        )
        is True
    )


def test_scenario_reconciles_committed_preemption_then_resumes_work():
    persistence, entities, engine, backend = _active_work()
    emergency_id = f"bay-emergency-scenario:{entities.work_order_id}:1"

    engine.preemptive_resources.request(
        backend,
        resource_name="maintenance_bay",
        request_id=emergency_id,
        requested_at=backend.now,
        priority=1,
        preempt=True,
    )
    backend.run_until(backend.now)

    assert persistence.entity("work_order", entities.work_order_id).state == "in_progress"
    assert any(
        result.preempting_request_id == emergency_id
        for result in persistence.resource_preemption_results()
    )

    assert mro.reconcile_scenario_emergency(
        persistence,
        engine,
        backend,
        entities=entities,
    )

    assert persistence.entity("work_order", entities.work_order_id).state == "in_progress"
    assert [r.request_id for r in persistence.preemptive_resource_reservations()] == [
        f"bay:{entities.work_order_id}"
    ]


def test_scenario_with_active_emergency_but_no_interrupted_work_returns_false():
    persistence, entities, engine, backend = _released_runtime()
    emergency_id = f"bay-emergency-scenario:{entities.work_order_id}:1"

    engine.preemptive_resources.request(
        backend,
        resource_name="maintenance_bay",
        request_id=emergency_id,
        requested_at=backend.now,
        priority=1,
        preempt=False,
    )
    backend.run_until(backend.now)

    assert persistence.entity("work_order", entities.work_order_id).state == "released"
    assert mro.reconcile_scenario_emergency(
        persistence,
        engine,
        backend,
        entities=entities,
    ) is False

from sose.backends.simpy import SimPyBackend
from sose.examples.mro.simulation import (
    ORIGIN,
    build_runtime,
    reconcile_emergency_interrupt,
    reconcile_emergency_resume,
    reconcile_start,
    seed_reference,
    seed_spare_parts,
)
from sose.persistence.memory import MemoryPersistence


def _active_work():
    persistence = MemoryPersistence()
    ids = seed_reference(persistence, quantity=1.0)
    _, engine = build_runtime(persistence)
    backend = SimPyBackend(origin=ORIGIN)
    engine.rebuild_backend(backend)
    seed_spare_parts(engine, backend, quantity=1.0)
    backend.run_until(ORIGIN.replace(hour=9))
    assert reconcile_start(
        persistence, engine, backend, entities=ids, quantity=1.0
    )
    return persistence, ids, engine, backend


def test_emergency_requires_active_work_and_owned_bay():
    persistence = MemoryPersistence()
    ids = seed_reference(persistence, quantity=1.0)
    _, engine = build_runtime(persistence)
    backend = SimPyBackend(origin=ORIGIN)
    engine.rebuild_backend(backend)

    assert reconcile_emergency_interrupt(
        persistence, engine, backend, entities=ids
    ) is False
    assert persistence.preemptive_resource_demands() == ()
    assert persistence.preemptive_resource_reservations() == ()
    assert persistence.resource_preemption_results() == ()


def test_emergency_preempts_bay_and_interrupts_work_order():
    persistence, ids, engine, backend = _active_work()

    assert reconcile_emergency_interrupt(
        persistence, engine, backend, entities=ids
    )

    assert persistence.entity("work_order", ids.work_order_id).state == "interrupted"
    result = persistence.resource_preemption_results()[0]
    assert result.displaced_request_id == f"bay:{ids.work_order_id}"
    assert result.preempting_request_id == f"bay-emergency:{ids.work_order_id}:1"


def test_reconcile_committed_preemption_after_crash_before_interrupt_transition():
    persistence, ids, engine, backend = _active_work()

    emergency_id = f"bay-emergency:{ids.work_order_id}:1"
    engine.preemptive_resources.request(
        backend,
        resource_name="maintenance_bay",
        request_id=emergency_id,
        requested_at=backend.now,
        priority=1,
        preempt=True,
    )
    backend.run_until(backend.now)

    assert persistence.entity("work_order", ids.work_order_id).state == "in_progress"
    assert persistence.resource_preemption_results()
    assert not any(
        r.request_id == f"bay:{ids.work_order_id}"
        for r in persistence.preemptive_resource_reservations()
    )

    _, restarted_engine = build_runtime(persistence, now=backend.now)
    restarted_backend = SimPyBackend(origin=backend.now)
    restarted_engine.rebuild_backend(restarted_backend)
    restarted_backend.run_until(backend.now)

    assert reconcile_emergency_interrupt(
        persistence,
        restarted_engine,
        restarted_backend,
        entities=ids,
    )
    assert persistence.entity("work_order", ids.work_order_id).state == "interrupted"


def test_emergency_resume_requires_reacquired_bay():
    persistence, ids, engine, backend = _active_work()
    reconcile_emergency_interrupt(persistence, engine, backend, entities=ids)

    assert reconcile_emergency_resume(
        persistence, engine, backend, entities=ids
    )

    assert persistence.entity("work_order", ids.work_order_id).state == "in_progress"
    assert [r.request_id for r in persistence.preemptive_resource_reservations()] == [
        f"bay:{ids.work_order_id}"
    ]


def test_stale_resume_on_non_interrupted_work_does_not_acquire_bay():
    persistence = MemoryPersistence()
    ids = seed_reference(persistence, quantity=1.0)
    _, engine = build_runtime(persistence)
    backend = SimPyBackend(origin=ORIGIN)
    engine.rebuild_backend(backend)

    assert reconcile_emergency_resume(
        persistence, engine, backend, entities=ids
    ) is False
    assert persistence.preemptive_resource_demands() == ()
    assert persistence.preemptive_resource_reservations() == ()


def test_same_work_order_can_be_interrupted_by_multiple_emergencies():
    persistence, ids, engine, backend = _active_work()

    assert reconcile_emergency_interrupt(
        persistence, engine, backend, entities=ids
    )
    assert reconcile_emergency_resume(
        persistence, engine, backend, entities=ids
    )
    assert reconcile_emergency_interrupt(
        persistence, engine, backend, entities=ids
    )

    results = persistence.resource_preemption_results()
    assert len(results) == 2
    assert [r.preempting_request_id for r in results] == [
        f"bay-emergency:{ids.work_order_id}:1",
        f"bay-emergency:{ids.work_order_id}:2",
    ]
    assert len({r.result_id for r in results}) == 2
    assert persistence.entity("work_order", ids.work_order_id).state == "interrupted"



def test_recovery_selects_second_active_preemption_after_prior_cycle():
    persistence, ids, engine, backend = _active_work()

    assert reconcile_emergency_interrupt(
        persistence, engine, backend, entities=ids
    )
    assert reconcile_emergency_resume(
        persistence, engine, backend, entities=ids
    )
    assert persistence.entity("work_order", ids.work_order_id).state == "in_progress"

    second_id = f"bay-emergency:{ids.work_order_id}:2"
    engine.preemptive_resources.request(
        backend,
        resource_name="maintenance_bay",
        request_id=second_id,
        requested_at=backend.now,
        priority=1,
        preempt=True,
    )
    backend.run_until(backend.now)

    results = persistence.resource_preemption_results()
    assert len(results) == 2
    assert results[-1].preempting_request_id == second_id
    assert persistence.entity("work_order", ids.work_order_id).state == "in_progress"

    _, restarted_engine = build_runtime(persistence, now=backend.now)
    restarted_backend = SimPyBackend(origin=backend.now)
    restarted_engine.rebuild_backend(restarted_backend)
    restarted_backend.run_until(backend.now)

    assert reconcile_emergency_interrupt(
        persistence,
        restarted_engine,
        restarted_backend,
        entities=ids,
    )
    assert persistence.entity("work_order", ids.work_order_id).state == "interrupted"
    assert len(persistence.resource_preemption_results()) == 2

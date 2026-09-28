from datetime import timedelta

from sose.backends.simpy import SimPyBackend
from sose.core.resources import DurableResourceManager
from sose.core.runtime import ResourceDefinition, StoreDefinition
from sose.core.stores import DurableStoreManager
from sose.examples.tutorial_job.simulation import (
    ORIGIN,
    build_runtime,
    seed_job,
    start_and_schedule_completion,
)
from sose.persistence.memory import MemoryPersistence
from sose.testing.restart import restart_reference_runtime_repeated


def _scheduled_batch_snapshot(count: int = 48):
    persistence = MemoryPersistence()
    jobs = [seed_job(persistence, job_key=f"job-{index:03d}") for index in range(count)]
    _, engine = build_runtime(persistence)
    backend = SimPyBackend(origin=ORIGIN)
    engine.rebuild_backend(backend)
    complete_at = ORIGIN + timedelta(hours=2)

    for job in jobs:
        start_and_schedule_completion(
            persistence,
            engine,
            job_id=job.id,
            complete_at=complete_at,
        )

    backend.run_until(complete_at)
    return (
        tuple(
            persistence.entity("tutorial_job", job.id)
            for job in jobs
        ),
        persistence.events(),
        persistence.simulation_position(),
    )


def test_repeated_rebuilds_do_not_mutate_pending_durable_truth():
    persistence = MemoryPersistence()
    job = seed_job(persistence)
    _, engine = build_runtime(persistence)
    backend = SimPyBackend(origin=ORIGIN)
    engine.rebuild_backend(backend)
    complete_at = ORIGIN + timedelta(hours=2)

    start_and_schedule_completion(
        persistence,
        engine,
        job_id=job.id,
        complete_at=complete_at,
    )
    before_work = persistence.scheduled_work()
    before_events = persistence.events()

    rebuilt = restart_reference_runtime_repeated(
        persistence,
        build_runtime,
        backend,
        backend_factory=SimPyBackend,
        count=8,
    )

    assert persistence.scheduled_work() == before_work
    assert persistence.events() == before_events
    rebuilt.backend.run_until(complete_at)
    completed = persistence.entity("tutorial_job", job.id)
    assert completed is not None and completed.state == "completed"


def test_consumed_work_stays_stale_after_repeated_restart():
    persistence = MemoryPersistence()
    job = seed_job(persistence)
    _, engine = build_runtime(persistence)
    backend = SimPyBackend(origin=ORIGIN)
    engine.rebuild_backend(backend)
    complete_at = ORIGIN + timedelta(hours=1)

    start_and_schedule_completion(
        persistence,
        engine,
        job_id=job.id,
        complete_at=complete_at,
    )
    stale = engine.scheduler.pending()[0]

    rebuilt = restart_reference_runtime_repeated(
        persistence,
        build_runtime,
        backend,
        backend_factory=SimPyBackend,
        count=3,
    )
    rebuilt.backend.run_until(complete_at)

    assert engine.dispatch_scheduled(stale) is False
    assert len(
        [
            event
            for event in persistence.events()
            if event.payload.get("trigger") == "finish"
        ]
    ) == 1


def test_same_seed_and_input_replay_large_scheduled_batch_deterministically():
    left = _scheduled_batch_snapshot()
    right = _scheduled_batch_snapshot()

    assert left == right
    assert all(entity is not None and entity.state == "completed" for entity in left[0])


def test_resource_queue_stress_preserves_fifo_with_equal_priority():
    persistence = MemoryPersistence()
    manager = DurableResourceManager(persistence)
    backend = SimPyBackend(origin=ORIGIN)
    with persistence.transaction() as uow:
        uow.save_resource_definition(ResourceDefinition("workers", capacity=4))
    manager.rebuild_backend(backend)

    granted: list[str] = []
    request_ids = [f"request-{index:03d}" for index in range(40)]
    for request_id in request_ids:
        manager.request(
            backend,
            resource_name="workers",
            request_id=request_id,
            requested_at=ORIGIN,
            priority=100,
            on_acquired=lambda reservation, request_id=request_id: granted.append(request_id),
        )
    backend.run_until(ORIGIN)

    while persistence.resource_reservations():
        for reservation in tuple(persistence.resource_reservations()):
            assert manager.release(backend, reservation.reservation_id)
        backend.run_until(ORIGIN)

    assert granted == request_ids
    assert persistence.resource_demands() == ()
    assert persistence.resource_reservations() == ()
    assert persistence.resource_release_intents() == ()


def test_priority_store_stress_preserves_priority_then_insertion_order():
    persistence = MemoryPersistence()
    manager = DurableStoreManager(persistence)
    backend = SimPyBackend(origin=ORIGIN)
    manager.define(StoreDefinition("dispatch", kind="priority"))
    manager.rebuild_backend(backend)

    count = 64
    for index in range(count):
        manager.put(
            backend,
            store_name="dispatch",
            item_id=f"item-{index:03d}",
            value=index,
            priority=index % 7,
            requested_at=ORIGIN,
        )
    backend.run_until(ORIGIN)

    selected = []
    for index in range(count):
        result = manager.ensure_selection(
            backend,
            store_name="dispatch",
            request_id=f"pick-{index:03d}",
            requested_at=ORIGIN,
        )
        assert result is not None
        selected.append(result.item.item_id)

    expected = [
        f"item-{index:03d}"
        for index in sorted(range(count), key=lambda value: (value % 7, value))
    ]
    assert selected == expected
    assert persistence.store_items() == ()
    assert persistence.store_get_requests() == ()
    assert len(persistence.store_get_results()) == count

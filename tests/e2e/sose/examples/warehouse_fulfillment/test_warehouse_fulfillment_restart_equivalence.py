from __future__ import annotations

from copy import deepcopy
from datetime import timedelta

from sose.backends.simpy import SimPyBackend
from sose.core.identity import deterministic_id
from sose.core.runtime import ResourceReleaseIntent
from sose.examples.warehouse_fulfillment.scenarios import ORIGIN
from sose.examples.warehouse_fulfillment.simulation import (
    allocate_order,
    build_runtime,
    pick_allocation,
    reconcile_fulfillment_services,
    seed_reference,
    service_task_id,
)
from sose.persistence.memory import MemoryPersistence
from sose.testing.restart import restart_reference_runtime


DURATION = timedelta(hours=1)


def _start_reference():
    persistence = MemoryPersistence()
    entities = seed_reference(
        persistence,
        picker_capacity=1,
        packing_station_capacity=1,
        shipping_dock_capacity=1,
    )
    _, engine = build_runtime(persistence)
    backend = SimPyBackend(origin=ORIGIN)
    engine.rebuild_backend(backend)
    assert allocate_order(persistence, engine, entities=entities)
    _reconcile(persistence, entities, engine, backend)
    return persistence, entities, engine, backend


def _reconcile(persistence, entities, engine, backend):
    reconcile_fulfillment_services(
        persistence,
        engine,
        backend,
        entities=entities,
        pick_duration=DURATION,
        pack_duration=DURATION,
        ship_duration=DURATION,
    )


def _restart(persistence, backend):
    return restart_reference_runtime(
        persistence,
        build_runtime,
        backend,
        backend_factory=SimPyBackend,
    )


def _order(persistence, entities):
    value = persistence.entity("warehouse_fulfillment_order", entities.order_id)
    assert value is not None
    return value


def _allocation_ids(persistence, entities) -> tuple[str, ...]:
    return tuple(str(value) for value in _order(persistence, entities).attributes["allocation_ids"])


def _task(persistence, stage: str, subject_id: str):
    value = persistence.entity(
        "warehouse_fulfillment_service_task",
        service_task_id(stage, subject_id),
    )
    assert value is not None
    return value


def _save(persistence, entity) -> None:
    with persistence.transaction() as uow:
        uow.save_entity(entity)


def _snapshot(persistence, entities) -> dict[str, object]:
    order = _order(persistence, entities)
    allocation_ids = _allocation_ids(persistence, entities)
    entity_keys = [
        ("warehouse_fulfillment_order", entities.order_id),
        *(("warehouse_inventory_lot", lot_id) for lot_id in entities.lot_ids),
        *(("warehouse_allocation", allocation_id) for allocation_id in allocation_ids),
        *(("warehouse_fulfillment_service_task", service_task_id("pick", allocation_id)) for allocation_id in allocation_ids),
        ("warehouse_fulfillment_service_task", service_task_id("pack", order.id)),
        ("warehouse_fulfillment_service_task", service_task_id("ship", order.id)),
    ]

    entities_snapshot = {}
    for entity_type, entity_id in entity_keys:
        entity = persistence.entity(entity_type, entity_id)
        assert entity is not None
        entities_snapshot[(entity_type, entity_id)] = (
            entity.state,
            deepcopy(entity.attributes),
            entity.version,
        )

    scheduled = tuple(persistence.scheduled_work())
    commands = tuple(
        persistence.command(work.command_id)
        for work in scheduled
    )
    return {
        "entities": entities_snapshot,
        "events": tuple(persistence.events()),
        "resource_definitions": tuple(persistence.resource_definitions()),
        "resource_demands": tuple(persistence.resource_demands()),
        "resource_reservations": tuple(persistence.resource_reservations()),
        "resource_release_intents": tuple(persistence.resource_release_intents()),
        "scheduled_work": scheduled,
        "commands": commands,
    }


def _run_complete(*, restart_each_boundary: bool):
    persistence, entities, engine, backend = _start_reference()

    for hour in range(1, 5):
        if restart_each_boundary:
            rebuilt = _restart(persistence, backend)
            engine = rebuilt.engine
            backend = rebuilt.backend
        backend.run_until(ORIGIN + timedelta(hours=hour))
        _reconcile(persistence, entities, engine, backend)

    assert _order(persistence, entities).state == "shipped"
    return _snapshot(persistence, entities)


def test_continuous_and_rebuilt_service_execution_have_equal_operational_snapshot():
    assert _run_complete(restart_each_boundary=False) == _run_complete(
        restart_each_boundary=True
    )


def test_restart_repairs_acquired_reservation_before_task_timing_and_schedule():
    persistence, entities, engine, backend = _start_reference()
    first_id = _allocation_ids(persistence, entities)[0]
    task = _task(persistence, "pick", first_id)

    assert task.state == "in_progress"
    assert len(persistence.resource_reservations()) == 1
    assert engine.scheduler.cancel_pending(
        entity_type=task.entity_type,
        entity_id=task.id,
        name="complete",
    )

    task.state = "queued"
    task.attributes["acquired_at"] = None
    task.attributes["completion_due_at"] = None
    task.attributes["reservation_id"] = None
    _save(persistence, task)

    rebuilt = _restart(persistence, backend)
    _reconcile(persistence, entities, rebuilt.engine, rebuilt.backend)

    repaired = _task(persistence, "pick", first_id)
    reservation = persistence.resource_reservations()[0]
    assert repaired.state == "in_progress"
    assert repaired.attributes["acquired_at"] == reservation.acquired_at.isoformat()
    assert repaired.attributes["reservation_id"] == reservation.reservation_id
    assert repaired.attributes["completion_due_at"] == (
        reservation.acquired_at + DURATION
    ).isoformat()
    pending = rebuilt.engine.scheduler.find_pending(
        entity_type=repaired.entity_type,
        entity_id=repaired.id,
        name="complete",
    )
    assert pending is not None
    assert pending.work.due_at == reservation.acquired_at + DURATION


def test_restart_repairs_in_progress_task_with_missing_completion_work():
    persistence, entities, engine, backend = _start_reference()
    first_id = _allocation_ids(persistence, entities)[0]
    task = _task(persistence, "pick", first_id)
    due_at = task.attributes["completion_due_at"]

    assert engine.scheduler.cancel_pending(
        entity_type=task.entity_type,
        entity_id=task.id,
        name="complete",
    )
    assert persistence.scheduled_work() == ()

    rebuilt = _restart(persistence, backend)
    _reconcile(persistence, entities, rebuilt.engine, rebuilt.backend)

    repaired = _task(persistence, "pick", first_id)
    pending = rebuilt.engine.scheduler.find_pending(
        entity_type=repaired.entity_type,
        entity_id=repaired.id,
        name="complete",
    )
    assert repaired.state == "in_progress"
    assert pending is not None
    assert pending.work.due_at.isoformat() == due_at


def test_restart_reconciles_completed_task_before_business_effect():
    persistence, entities, _, backend = _start_reference()
    first_id = _allocation_ids(persistence, entities)[0]

    backend.run_until(ORIGIN + DURATION)
    completed = _task(persistence, "pick", first_id)
    allocation = persistence.entity("warehouse_allocation", first_id)
    assert completed.state == "completed"
    assert completed.attributes["business_applied"] is False
    assert allocation is not None and allocation.state == "committed"

    rebuilt = _restart(persistence, backend)
    _reconcile(persistence, entities, rebuilt.engine, rebuilt.backend)

    repaired = _task(persistence, "pick", first_id)
    allocation = persistence.entity("warehouse_allocation", first_id)
    assert allocation is not None and allocation.state == "picked"
    assert repaired.attributes["business_applied"] is True
    assert repaired.attributes["resource_released"] is True

    occurrence_count = len(_order(persistence, entities).attributes["occurrence_ids"])
    _reconcile(persistence, entities, rebuilt.engine, rebuilt.backend)
    assert len(_order(persistence, entities).attributes["occurrence_ids"]) == occurrence_count


def test_restart_cleans_capacity_after_business_effect_was_already_committed():
    persistence, entities, _, backend = _start_reference()
    first_id = _allocation_ids(persistence, entities)[0]

    backend.run_until(ORIGIN + DURATION)
    task = _task(persistence, "pick", first_id)
    assert task.state == "completed"

    # Fault injection: business effect committed, process dies before resource cleanup.
    _, engine = build_runtime(persistence, now=backend.now)
    pick_allocation(
        persistence,
        engine,
        entities=entities,
        allocation_id_value=first_id,
    )
    task = _task(persistence, "pick", first_id)
    task.attributes["business_applied"] = True
    _save(persistence, task)
    before_occurrences = tuple(_order(persistence, entities).attributes["occurrence_ids"])

    rebuilt = _restart(persistence, backend)
    _reconcile(persistence, entities, rebuilt.engine, rebuilt.backend)

    repaired = _task(persistence, "pick", first_id)
    assert repaired.attributes["business_applied"] is True
    assert repaired.attributes["resource_released"] is True
    assert tuple(_order(persistence, entities).attributes["occurrence_ids"]) == before_occurrences
    assert all(
        reservation.request_id != str(repaired.attributes["request_id"])
        for reservation in persistence.resource_reservations()
    )


def test_restart_finalizes_interrupted_release_intent_without_duplicate_business_effect():
    persistence, entities, _, backend = _start_reference()
    first_id = _allocation_ids(persistence, entities)[0]

    backend.run_until(ORIGIN + DURATION)
    task = _task(persistence, "pick", first_id)
    _, engine = build_runtime(persistence, now=backend.now)
    pick_allocation(
        persistence,
        engine,
        entities=entities,
        allocation_id_value=first_id,
    )
    task = _task(persistence, "pick", first_id)
    task.attributes["business_applied"] = True
    _save(persistence, task)

    reservation = next(
        reservation
        for reservation in persistence.resource_reservations()
        if reservation.request_id == str(task.attributes["request_id"])
    )
    intent = ResourceReleaseIntent(
        intent_id=deterministic_id("resource-release", reservation.reservation_id),
        reservation_id=reservation.reservation_id,
        resource_name=reservation.resource_name,
        requested_at=backend.now,
    )
    with persistence.transaction() as uow:
        uow.save_resource_release_intent(intent)

    before_occurrences = tuple(_order(persistence, entities).attributes["occurrence_ids"])
    rebuilt = _restart(persistence, backend)
    assert persistence.resource_release_intents() == ()
    assert all(
        value.reservation_id != reservation.reservation_id
        for value in persistence.resource_reservations()
    )

    _reconcile(persistence, entities, rebuilt.engine, rebuilt.backend)
    repaired = _task(persistence, "pick", first_id)
    assert repaired.attributes["resource_released"] is True
    assert tuple(_order(persistence, entities).attributes["occurrence_ids"]) == before_occurrences


def test_terminal_flow_leaves_no_resource_or_schedule_leaks():
    snapshot = _run_complete(restart_each_boundary=True)

    assert snapshot["resource_demands"] == ()
    assert snapshot["resource_reservations"] == ()
    assert snapshot["resource_release_intents"] == ()
    assert snapshot["scheduled_work"] == ()
    assert snapshot["commands"] == ()

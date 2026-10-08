from sose.backends.simpy import SimPyBackend
from sose.examples.warehouse_fulfillment.scenarios import ORIGIN
from sose.examples.warehouse_fulfillment.simulation import (
    allocate_order,
    build_runtime,
    pack_order,
    pick_order,
    seed_reference,
    ship_order,
)
from sose.persistence.memory import MemoryPersistence
from sose.testing.restart import restart_reference_runtime


def test_committed_allocation_and_partial_pick_resume_after_restart():
    persistence = MemoryPersistence()
    entities = seed_reference(persistence)
    _, engine = build_runtime(persistence)
    backend = SimPyBackend(origin=ORIGIN)
    engine.rebuild_backend(backend)

    assert allocate_order(persistence, engine, entities=entities)
    order = persistence.entity("warehouse_fulfillment_order", entities.order_id)
    assert order is not None
    first_allocation_id = str(order.attributes["allocation_ids"][0])

    assert pick_order(
        persistence,
        engine,
        backend,
        entities=entities,
    ) is False
    first_due = min(work.due_at for work in persistence.scheduled_work())
    backend.run_until(first_due)
    assert pick_order(
        persistence,
        engine,
        backend,
        entities=entities,
    ) is False

    first = persistence.entity("warehouse_allocation", first_allocation_id)
    assert first is not None and first.state == "picked"

    rebuilt = restart_reference_runtime(
        persistence,
        build_runtime,
        backend,
        backend_factory=SimPyBackend,
    )
    while not pick_order(
        persistence,
        rebuilt.engine,
        rebuilt.backend,
        entities=entities,
    ):
        due = min(work.due_at for work in persistence.scheduled_work())
        rebuilt.backend.run_until(due)

    while not pack_order(
        persistence,
        rebuilt.engine,
        rebuilt.backend,
        entities=entities,
    ):
        due = min(work.due_at for work in persistence.scheduled_work())
        rebuilt.backend.run_until(due)

    while not ship_order(
        persistence,
        rebuilt.engine,
        rebuilt.backend,
        entities=entities,
    ):
        due = min(work.due_at for work in persistence.scheduled_work())
        rebuilt.backend.run_until(due)

    order = persistence.entity("warehouse_fulfillment_order", entities.order_id)
    assert order is not None and order.state == "shipped"
    assert len(order.attributes["occurrence_ids"]) == 4
    assert persistence.resource_reservations() == ()
    assert persistence.scheduled_work() == ()

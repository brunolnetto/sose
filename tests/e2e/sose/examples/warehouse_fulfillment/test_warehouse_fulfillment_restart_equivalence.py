from sose.backends.simpy import SimPyBackend
from sose.examples.warehouse_fulfillment.scenarios import ORIGIN
from sose.examples.warehouse_fulfillment.simulation import (
    allocate_order,
    build_runtime,
    pack_order,
    pick_allocation,
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
    first_allocation_id = order.attributes["allocation_ids"][0]
    pick_allocation(
        persistence,
        engine,
        entities=entities,
        allocation_id_value=first_allocation_id,
    )

    first = persistence.entity("warehouse_allocation", first_allocation_id)
    assert first is not None and first.state == "picked"

    rebuilt = restart_reference_runtime(
        persistence,
        build_runtime,
        backend,
        backend_factory=SimPyBackend,
    )
    pick_order(persistence, rebuilt.engine, entities=entities)
    pack_order(persistence, rebuilt.engine, entities=entities)
    ship_order(persistence, rebuilt.engine, entities=entities)

    order = persistence.entity("warehouse_fulfillment_order", entities.order_id)
    assert order is not None and order.state == "shipped"
    assert len(order.attributes["occurrence_ids"]) == 4
    assert persistence.resource_reservations() == ()
    assert persistence.scheduled_work() == ()

from sose.backends.simpy import SimPyBackend
from sose.domain.outbox import DomainMutationOutbox
from sose.domain.projector import DomainWarehouseProjector
from sose.domain.warehouse import MemoryDomainWarehouse
from sose.examples.mro.simulation import ORIGIN, build_runtime, reconcile_complete, reconcile_start, release_capacity, seed_reference, seed_spare_parts
from sose.examples.mro.warehouse import sync_mro_domain
from sose.persistence.sqlite_incremental import SQLiteIncrementalPersistence

def _run_to_closed(engine_store, warehouse):
    ids = seed_reference(engine_store)
    sync_mro_domain(engine_store, warehouse, ids)
    _, engine = build_runtime(engine_store)
    backend = SimPyBackend(origin=ORIGIN)
    engine.rebuild_backend(backend)
    backend.run_until(ORIGIN.replace(hour=9))
    seed_spare_parts(engine, backend, quantity=1.0)
    assert reconcile_start(engine_store, engine, backend, entities=ids, quantity=1.0)
    sync_mro_domain(engine_store, warehouse, ids)
    reconcile_complete(engine_store, engine, entities=ids)
    release_capacity(engine_store, engine, backend, entities=ids)
    sync_mro_domain(engine_store, warehouse, ids)
    return ids

def _world(warehouse, ids):
    return (warehouse.entity('work_order', ids.work_order_id), warehouse.entity('part_demand', ids.part_demand_id))

def test_mro_domain_warehouse_matches_continuous_and_restarted_execution(tmp_path):
    continuous_engine = SQLiteIncrementalPersistence(tmp_path / 'continuous-engine.db')
    continuous_world = MemoryDomainWarehouse()
    continuous_ids = _run_to_closed(continuous_engine, continuous_world)
    expected = _world(continuous_world, continuous_ids)
    assert expected[0].state == 'closed'
    assert expected[1].state == 'consumed'
    continuous_engine.close()

    path = tmp_path / 'restarted-engine.db'
    first = SQLiteIncrementalPersistence(path)
    ids = seed_reference(first)
    restarted_world = MemoryDomainWarehouse()
    sync_mro_domain(first, restarted_world, ids)
    _, engine = build_runtime(first)
    backend = SimPyBackend(origin=ORIGIN)
    engine.rebuild_backend(backend)
    backend.run_until(ORIGIN.replace(hour=9))
    seed_spare_parts(engine, backend, quantity=1.0)
    assert reconcile_start(first, engine, backend, entities=ids, quantity=1.0)
    projector = DomainWarehouseProjector(first, restarted_world)
    projector.prepare_entities((('work_order', ids.work_order_id), ('part_demand', ids.part_demand_id)))
    assert first.domain_deliveries()
    first.close()

    reopened = SQLiteIncrementalPersistence(path)
    assert DomainMutationOutbox(reopened, restarted_world).flush() == 2
    position = reopened.simulation_position()
    now = position.logical_time if position is not None else ORIGIN
    tick = position.logical_tick if position is not None else 0
    _, engine = build_runtime(reopened, now=now, tick=tick)
    backend = SimPyBackend(origin=now)
    engine.rebuild_backend(backend)
    backend.run_until(now)
    reconcile_complete(reopened, engine, entities=ids)
    release_capacity(reopened, engine, backend, entities=ids)
    sync_mro_domain(reopened, restarted_world, ids)
    assert _world(restarted_world, ids) == expected
    assert reopened.domain_deliveries() == ()
    reopened.close()

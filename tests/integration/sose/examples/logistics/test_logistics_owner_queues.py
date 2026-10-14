"""PC6: owner-qualified Logistics FIFO queues never steal another owner's item.

Both engines intentionally share one durable persistence object. The backend
rebuild sees all definitions, but a cross-owner StoreGet cannot consume A's FIFO
head or manufacture a receipt for B.
"""
from sose.backends.simpy import SimPyBackend
from sose.examples.logistics import simulation as logistics
from sose.persistence.memory import MemoryPersistence


def test_independent_organizations_preserve_distinct_durable_fifo_heads():
    store = MemoryPersistence()
    logistics.seed_reference(store, owner_id="org-a", instance_key="shipment-a")
    logistics.seed_reference(store, owner_id="org-b", instance_key="shipment-b")
    definitions = {definition.name: definition for definition in store.store_definitions()}
    expected = {
        "org-a/origin_hub_queue", "org-a/destination_hub_queue",
        "org-b/origin_hub_queue", "org-b/destination_hub_queue",
    }
    assert set(definitions) == expected
    assert all(definitions[name].kind == "fifo" for name in expected)

    _, engine_a = logistics.build_runtime(store, owner_id="org-a")
    backend_a = SimPyBackend(origin=logistics.ORIGIN)
    engine_a.stores.rebuild_backend(backend_a)
    engine_a.stores.put(
        backend_a, store_name="org-a/origin_hub_queue",
        item_id="origin-queue:shipment-a", value={"shipment_id": "shipment-a"},
        requested_at=backend_a.now,
    )
    backend_a.run_until(backend_a.now)
    assert any(item.item_id == "origin-queue:shipment-a" for item in store.store_items())

    _, engine_b = logistics.build_runtime(store, owner_id="org-b")
    backend_b = SimPyBackend(origin=logistics.ORIGIN)
    engine_b.stores.rebuild_backend(backend_b)
    assert engine_b.stores.ensure_selection(
        backend_b, store_name="org-b/origin_hub_queue",
        request_id="origin-dequeue:shipment-b", requested_at=backend_b.now,
    ) is None
    assert not store.store_get_results()
    assert any(item.item_id == "origin-queue:shipment-a" for item in store.store_items())

    engine_b.stores.put(
        backend_b, store_name="org-b/origin_hub_queue",
        item_id="origin-queue:shipment-b", value={"shipment_id": "shipment-b"},
        requested_at=backend_b.now,
    )
    backend_b.run_until(backend_b.now)
    result_b = engine_b.stores.selection("origin-dequeue:shipment-b")
    assert result_b is not None
    assert result_b.item.item_id == "origin-queue:shipment-b"
    assert result_b.item.store_name == "org-b/origin_hub_queue"
    assert any(item.item_id == "origin-queue:shipment-a" for item in store.store_items())

    result_a = engine_a.stores.ensure_selection(
        backend_a, store_name="org-a/origin_hub_queue",
        request_id="origin-dequeue:shipment-a", requested_at=backend_a.now,
    )
    assert result_a is not None
    assert result_a.item.item_id == "origin-queue:shipment-a"
    assert result_a.item.store_name == "org-a/origin_hub_queue"
    assert not store.store_items()

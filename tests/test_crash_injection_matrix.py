from __future__ import annotations

from datetime import datetime, timezone

import pytest

from sose.backends.simpy import SimPyBackend
from sose.core.containers import DurableContainerManager
from sose.core.resources import DurableResourceManager
from sose.core.runtime import ContainerDefinition, ResourceDefinition, StoreDefinition
from sose.core.stores import DurableStoreManager
from sose.persistence.memory import MemoryPersistence
from tests.support.faults import FaultInjector, InjectedCrash

NOW = datetime(2026, 1, 1, 8, tzinfo=timezone.utc)


class CrashBeforeResourceSubmitBackend:
    def __init__(self, injector: FaultInjector):
        self.now = NOW
        self.injector = injector

    def request_resource(self, *args, **kwargs):
        self.injector.hit("resource.after_demand_before_backend")


class CrashOnStorePutBackend:
    def __init__(self, injector: FaultInjector):
        self.now = NOW
        self.injector = injector

    def put_store(self, *args, **kwargs):
        self.injector.hit("store.after_put_intent_before_backend")


class CrashOnContainerGetBackend:
    def __init__(self, injector: FaultInjector):
        self.now = NOW
        self.injector = injector

    def get_container(self, *args, **kwargs):
        self.injector.hit("container.after_intent_before_backend")


def test_resource_crash_after_demand_persistence_is_recoverable():
    store = MemoryPersistence()
    with store.transaction() as uow:
        uow.save_resource_definition(ResourceDefinition("bay", capacity=1))

    injector = FaultInjector()
    injector.arm("resource.after_demand_before_backend")

    with pytest.raises(InjectedCrash):
        DurableResourceManager(store).request(
            CrashBeforeResourceSubmitBackend(injector),
            resource_name="bay",
            request_id="r1",
            requested_at=NOW,
        )

    assert [d.request_id for d in store.resource_demands()] == ["r1"]


def test_store_crash_after_put_intent_is_recoverable():
    store = MemoryPersistence()
    manager = DurableStoreManager(store)
    manager.define(StoreDefinition("inbox"))

    injector = FaultInjector()
    injector.arm("store.after_put_intent_before_backend")

    with pytest.raises(InjectedCrash):
        manager.put(
            CrashOnStorePutBackend(injector),
            store_name="inbox",
            item_id="item-1",
            value=1,
            requested_at=NOW,
        )

    assert [i.item_id for i in store.store_put_intents()] == ["item-1"]


def test_container_crash_after_intent_is_recoverable():
    store = MemoryPersistence()
    manager = DurableContainerManager(store)
    manager.define(ContainerDefinition("fuel", capacity=10.0, initial=5.0))

    injector = FaultInjector()
    injector.arm("container.after_intent_before_backend")

    with pytest.raises(InjectedCrash):
        manager.get(
            CrashOnContainerGetBackend(injector),
            container_name="fuel",
            request_id="consume",
            amount=2.0,
            requested_at=NOW,
        )

    assert [i.request_id for i in store.container_operation_intents()] == ["consume"]


def test_recovery_matrix_replays_all_three_surviving_intents():
    store = MemoryPersistence()
    with store.transaction() as uow:
        uow.save_resource_definition(ResourceDefinition("bay", capacity=1))

    resource_manager = DurableResourceManager(store)
    store_manager = DurableStoreManager(store)
    store_manager.define(StoreDefinition("inbox"))
    container_manager = DurableContainerManager(store)
    container_manager.define(ContainerDefinition("fuel", capacity=10.0, initial=5.0))

    injectors = [FaultInjector(), FaultInjector(), FaultInjector()]
    injectors[0].arm("resource.after_demand_before_backend")
    injectors[1].arm("store.after_put_intent_before_backend")
    injectors[2].arm("container.after_intent_before_backend")

    with pytest.raises(InjectedCrash):
        resource_manager.request(
            CrashBeforeResourceSubmitBackend(injectors[0]),
            resource_name="bay",
            request_id="r1",
            requested_at=NOW,
        )
    with pytest.raises(InjectedCrash):
        store_manager.put(
            CrashOnStorePutBackend(injectors[1]),
            store_name="inbox",
            item_id="item-1",
            value=1,
            requested_at=NOW,
        )
    with pytest.raises(InjectedCrash):
        container_manager.get(
            CrashOnContainerGetBackend(injectors[2]),
            container_name="fuel",
            request_id="consume",
            amount=2.0,
            requested_at=NOW,
        )

    backend = SimPyBackend(origin=NOW)
    resource_manager = DurableResourceManager(store)
    store_manager = DurableStoreManager(store)
    container_manager = DurableContainerManager(store)

    resource_manager.rebuild_backend(backend)
    store_manager.rebuild_backend(backend)
    container_manager.rebuild_backend(backend)
    backend.run_until(NOW)

    assert [r.request_id for r in store.resource_reservations()] == ["r1"]
    assert [i.item_id for i in store.store_items()] == ["item-1"]
    assert store.container_states()[0].level == 3.0
    assert [r.request_id for r in store.container_operation_results()] == ["consume"]


def test_fault_injector_supports_nth_occurrence_and_wrapped_callbacks():
    injector = FaultInjector()
    injector.arm("checkpoint", occurrence=2)
    seen = []

    wrapped = injector.after("checkpoint", lambda value: seen.append(value) or value)

    assert wrapped(1) == 1
    with pytest.raises(InjectedCrash, match="checkpoint"):
        wrapped(2)

    assert seen == [1, 2]

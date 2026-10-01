from __future__ import annotations

from sose.core.runtime import StoreDefinition
from sose.domain.config import DomainDefinition
from sose.core.stores import DurableStoreManager

from .common import CanonicalConfig, build_runtime, seed_case, transition


class ProducerConsumerConfig(CanonicalConfig):
    participants: int = 4
    capacity: int = 2


def seed(persistence, config):
    stores = DurableStoreManager(persistence)
    if not persistence.store_definitions():
        stores.define(StoreDefinition("buffer", kind="fifo", capacity=config.capacity))
    return seed_case(persistence, name="producer_consumer", attributes={"capacity": config.capacity})


def reconcile(persistence, engine, backend, config, case):
    if not config.enabled:
        return
    current = persistence.entity("canonical_case", case.id)
    if current.state == "ready":
        stores = DurableStoreManager(persistence)
        for index in range(config.participants):
            stores.put(backend, store_name="buffer", item_id=f"item-{index}",
                       value={"producer": index}, requested_at=engine.context.clock.now)
        transition(engine, current, "advance")
    elif current.state == "active":
        stores = DurableStoreManager(persistence)
        for index in range(config.participants):
            stores.ensure_selection(backend, store_name="buffer",
                                    request_id=f"consumer-{index}",
                                    requested_at=engine.context.clock.now)
        if len(persistence.store_get_results()) == config.participants:
            transition(engine, current, "finish")


definition = DomainDefinition(
    name="producer_consumer",
    description="Canonical bounded-buffer producer/consumer problem demonstrating durable Store backpressure.",
    config_model=ProducerConsumerConfig, build_runtime=build_runtime, seed=seed,
    reconcile_tick=reconcile,
    runtime_mutable_fields=frozenset({"tick_step", "random_seed", "enabled"}),
)

from __future__ import annotations

from sose.core.runtime import StoreDefinition
from sose.domain.config import DomainDefinition
from sose.core.stores import DurableStoreManager

from .common import CanonicalConfig, build_runtime, resolve_tick_action, seed_case, transition


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
    candidate = current.state if current.state in {"ready", "active"} else "noop"
    action = resolve_tick_action(
        persistence,
        backend,
        canonical="producer_consumer",
        logical_tick=engine.context.clock.tick,
        candidate=candidate,
        requested_at=engine.context.clock.now,
    )

    if action == "ready":
        if current.state != "ready":
            return
        stores = DurableStoreManager(persistence)
        for index in range(config.participants):
            stores.ensure_put(
                backend,
                store_name="buffer",
                item_id=f"item-{index}",
                value={"producer": index},
                requested_at=engine.context.clock.now,
            )
        transition(engine, current, "advance")
        return

    if action == "active":
        if current.state != "active":
            return
        stores = DurableStoreManager(persistence)
        for index in range(config.participants):
            stores.ensure_selection(
                backend,
                store_name="buffer",
                request_id=f"consumer-{index}",
                requested_at=engine.context.clock.now,
            )
        if len(
            [
                result
                for result in persistence.store_get_results()
                if result.store_name == "buffer"
            ]
        ) == config.participants:
            transition(engine, current, "finish")


definition = DomainDefinition(
    name="producer_consumer",
    kind="canonical",
    description="Canonical bounded-buffer producer/consumer problem demonstrating durable Store backpressure.",
    config_model=ProducerConsumerConfig, build_runtime=build_runtime, seed=seed,
    reconcile_tick=reconcile,
    runtime_mutable_fields=frozenset({"tick_step", "random_seed", "enabled"}),
)

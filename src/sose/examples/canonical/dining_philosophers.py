from __future__ import annotations

from sose.core.resources import DurableResourceManager
from sose.core.runtime import ResourceDefinition
from sose.domain.config import DomainDefinition

from .common import CanonicalConfig, build_runtime, seed_case, transition


class DiningPhilosophersConfig(CanonicalConfig):
    participants: int = 5


def seed(persistence, config):
    if not persistence.resource_definitions():
        with persistence.transaction() as uow:
            for index in range(config.participants):
                uow.save_resource_definition(ResourceDefinition(f"fork-{index}", 1))
    return seed_case(persistence, name="dining_philosophers",
                     attributes={"philosophers": config.participants})


def reconcile(persistence, engine, backend, config, case):
    current = persistence.entity("canonical_case", case.id)
    resources = DurableResourceManager(persistence)
    if current.state == "ready":
        # Ordered acquisition removes circular wait while retaining contention.
        for philosopher in range(config.participants):
            left, right = sorted((philosopher, (philosopher + 1) % config.participants))
            resources.ensure_requested(backend, resource_name=f"fork-{left}",
                request_id=f"p{philosopher}-fork-{left}", requested_at=engine.context.clock.now)
            resources.ensure_requested(backend, resource_name=f"fork-{right}",
                request_id=f"p{philosopher}-fork-{right}", requested_at=engine.context.clock.now)
        transition(engine, current, "advance")
    elif current.state == "active":
        reservations = list(persistence.resource_reservations())
        if reservations:
            resources.release(backend, reservations[0].reservation_id)
        if not persistence.resource_demands() and not persistence.resource_reservations():
            transition(engine, persistence.entity("canonical_case", case.id), "finish")


definition = DomainDefinition(
    name="dining_philosophers",
    description="Canonical dining-philosophers problem demonstrating ordered durable Resource acquisition and contention.",
    config_model=DiningPhilosophersConfig, build_runtime=build_runtime, seed=seed,
    reconcile_tick=reconcile,
    runtime_mutable_fields=frozenset({"tick_step", "random_seed"}),
)

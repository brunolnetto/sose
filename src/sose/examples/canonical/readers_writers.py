from __future__ import annotations

from sose.core.resources import DurableResourceManager
from sose.core.runtime import ResourceDefinition
from sose.domain.config import DomainDefinition

from .common import CanonicalConfig, build_runtime, seed_case, transition


class ReadersWritersConfig(CanonicalConfig):
    participants: int = 4
    readers: int = 3


def seed(persistence, config):
    if not persistence.resource_definitions():
        with persistence.transaction() as uow:
            uow.save_resource_definition(ResourceDefinition("document", 1))
    return seed_case(persistence, name="readers_writers",
                     attributes={"readers": config.readers, "writers": config.participants - config.readers})


def reconcile(persistence, engine, backend, config, case):
    current = persistence.entity("canonical_case", case.id)
    resources = DurableResourceManager(persistence)
    if current.state == "ready":
        # A single ownership token makes exclusion explicit; reader requests
        # precede writers in this minimal fairness demonstration.
        for index in range(config.readers):
            resources.ensure_requested(backend, resource_name="document",
                request_id=f"reader-{index}", requested_at=engine.context.clock.now, priority=10)
        for index in range(config.participants - config.readers):
            resources.ensure_requested(backend, resource_name="document",
                request_id=f"writer-{index}", requested_at=engine.context.clock.now, priority=20)
        transition(engine, current, "advance")
    elif current.state == "active":
        reservations = list(persistence.resource_reservations())
        if reservations:
            resources.release(backend, reservations[0].reservation_id)
        if not persistence.resource_demands() and not persistence.resource_reservations():
            transition(engine, persistence.entity("canonical_case", case.id), "finish")


definition = DomainDefinition(
    name="readers_writers",
    description="Canonical readers/writers ownership problem demonstrating queued durable exclusion and priority.",
    config_model=ReadersWritersConfig, build_runtime=build_runtime, seed=seed,
    reconcile_tick=reconcile,
    runtime_mutable_fields=frozenset({"tick_step", "random_seed"}),
)

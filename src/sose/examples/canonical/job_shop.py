from __future__ import annotations

from sose.core.resources import DurableResourceManager
from sose.core.runtime import ResourceDefinition
from sose.domain.config import DomainDefinition
from pydantic import Field

from .common import CanonicalConfig, build_runtime, seed_case, transition


class JobShopConfig(CanonicalConfig):
    participants: int = 3
    machines: int = Field(default=2, ge=1)


def seed(persistence, config):
    if not persistence.resource_definitions():
        with persistence.transaction() as uow:
            for index in range(config.machines):
                uow.save_resource_definition(ResourceDefinition(f"machine-{index}", 1))
    return seed_case(persistence, name="job_shop",
                     attributes={"jobs": config.participants, "machines": config.machines})


def reconcile(persistence, engine, backend, config, case):
    if not config.enabled:
        return
    current = persistence.entity("canonical_case", case.id)
    resources = engine.resources
    if current.state == "ready":
        # First operation of each route; subsequent operations are admitted as
        # earlier ownership is released, preserving deterministic precedence.
        for job in range(config.participants):
            resources.ensure_requested(backend, resource_name=f"machine-{job % config.machines}",
                request_id=f"job-{job}-op-0", requested_at=engine.context.clock.now)
        transition(engine, current, "advance")
    elif current.state == "active":
        reservations = list(persistence.resource_reservations())
        if reservations:
            resources.release(backend, reservations[0].reservation_id)
        if not persistence.resource_demands() and not persistence.resource_reservations():
            transition(engine, persistence.entity("canonical_case", case.id), "finish")


definition = DomainDefinition(
    name="job_shop",
    description="Canonical job-shop scheduling problem demonstrating deterministic multi-machine Resource contention.",
    config_model=JobShopConfig, build_runtime=build_runtime, seed=seed,
    reconcile_tick=reconcile,
    runtime_mutable_fields=frozenset({"tick_step", "random_seed", "enabled"}),
)

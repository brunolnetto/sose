from __future__ import annotations

from sose.core.resources import DurableResourceManager
from sose.core.runtime import ResourceDefinition
from sose.domain.config import DomainDefinition

from .common import CanonicalConfig, build_runtime, seed_case, transition


class SleepingBarberConfig(CanonicalConfig):
    participants: int = 5
    capacity: int = 1
    waiting_chairs: int = 3


def seed(persistence, config):
    if not persistence.resource_definitions():
        with persistence.transaction() as uow:
            uow.save_resource_definition(ResourceDefinition("barber", config.capacity))
    return seed_case(persistence, name="sleeping_barber",
                     attributes={"customers": config.participants, "waiting_chairs": config.waiting_chairs})


def reconcile(persistence, engine, backend, config, case):
    current = persistence.entity("canonical_case", case.id)
    resources = DurableResourceManager(persistence)
    if current.state == "ready":
        accepted = min(config.participants, config.waiting_chairs + config.capacity)
        for index in range(accepted):
            resources.ensure_requested(backend, resource_name="barber",
                request_id=f"customer-{index}", requested_at=engine.context.clock.now)
        transition(engine, current, "advance")
    elif current.state == "active":
        reservations = list(persistence.resource_reservations())
        if reservations:
            resources.release(backend, reservations[0].reservation_id)
        if not persistence.resource_demands() and not persistence.resource_reservations():
            transition(engine, persistence.entity("canonical_case", case.id), "finish")


definition = DomainDefinition(
    name="sleeping_barber",
    description="Canonical sleeping-barber queue demonstrating capacity, waiting-room admission, and durable Resource ownership.",
    config_model=SleepingBarberConfig, build_runtime=build_runtime, seed=seed,
    reconcile_tick=reconcile,
    runtime_mutable_fields=frozenset({"tick_step", "random_seed"}),
)

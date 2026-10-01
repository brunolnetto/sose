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
    if not config.enabled:
        return
    current = persistence.entity("canonical_case", case.id)
    resources = engine.resources
    if current.state == "ready":
        # Request only the lower-ranked fork first. The second request is
        # admitted only after durable ownership of the first, implementing a
        # real resource hierarchy rather than merely sorting simultaneous asks.
        for philosopher in range(config.participants):
            first, _ = sorted((philosopher, (philosopher + 1) % config.participants))
            resources.ensure_requested(backend, resource_name=f"fork-{first}",
                request_id=f"p{philosopher}-fork-{first}", requested_at=engine.context.clock.now)
        transition(engine, current, "advance")
    elif current.state == "active":
        reservations = list(persistence.resource_reservations())
        by_request = {reservation.request_id: reservation for reservation in reservations}
        for philosopher in range(config.participants):
            first, second = sorted((philosopher, (philosopher + 1) % config.participants))
            first_id = f"p{philosopher}-fork-{first}"
            second_id = f"p{philosopher}-fork-{second}"
            if first_id in by_request and second_id not in by_request:
                resources.ensure_requested(backend, resource_name=f"fork-{second}",
                    request_id=second_id, requested_at=engine.context.clock.now)

        reservations = list(persistence.resource_reservations())
        by_request = {reservation.request_id: reservation for reservation in reservations}
        for philosopher in range(config.participants):
            first, second = sorted((philosopher, (philosopher + 1) % config.participants))
            first_id = f"p{philosopher}-fork-{first}"
            second_id = f"p{philosopher}-fork-{second}"
            if first_id in by_request and second_id in by_request:
                resources.release(backend, by_request[first_id].reservation_id)
                resources.release(backend, by_request[second_id].reservation_id)
                break
            if first_id not in by_request and second_id in by_request:
                # Recovery after a crash between the two durable releases.
                resources.release(backend, by_request[second_id].reservation_id)
                break
        if not persistence.resource_demands() and not persistence.resource_reservations():
            transition(engine, persistence.entity("canonical_case", case.id), "finish")


definition = DomainDefinition(
    name="dining_philosophers",
    kind="canonical",
    description="Canonical dining-philosophers problem demonstrating ordered durable Resource acquisition and contention.",
    config_model=DiningPhilosophersConfig, build_runtime=build_runtime, seed=seed,
    reconcile_tick=reconcile,
    runtime_mutable_fields=frozenset({"tick_step", "random_seed", "enabled"}),
)

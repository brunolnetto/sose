from __future__ import annotations

from sose.core.resources import DurableResourceManager
from sose.core.runtime import ResourceDefinition
from sose.domain.config import DomainDefinition

from .common import CanonicalConfig, build_runtime, resolve_tick_action, seed_case, transition


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
        candidate = "noop"
        for philosopher in range(config.participants):
            first, second = sorted((philosopher, (philosopher + 1) % config.participants))
            first_id = f"p{philosopher}-fork-{first}"
            second_id = f"p{philosopher}-fork-{second}"
            if (
                resources.reservation_for(first_id) is not None
                or resources.reservation_for(second_id) is not None
            ):
                candidate = f"p{philosopher}"
                break

        action = resolve_tick_action(
            persistence,
            backend,
            canonical="dining_philosophers",
            logical_tick=engine.context.clock.tick,
            candidate=candidate,
            requested_at=engine.context.clock.now,
        )
        if action != "noop":
            philosopher = int(action[1:])
            first, second = sorted((philosopher, (philosopher + 1) % config.participants))
            first_id = f"p{philosopher}-fork-{first}"
            second_id = f"p{philosopher}-fork-{second}"

            first_reservation = resources.reservation_for(first_id)
            second_reservation = resources.reservation_for(second_id)
            if first_reservation is not None and not resources.has_request(second_id):
                resources.ensure_requested(
                    backend,
                    resource_name=f"fork-{second}",
                    request_id=second_id,
                    requested_at=engine.context.clock.now,
                )
                second_reservation = resources.reservation_for(second_id)

            if first_reservation is not None and second_reservation is not None:
                resources.release(backend, first_reservation.reservation_id)
                remaining_second = resources.reservation_for(second_id)
                if remaining_second is not None:
                    resources.release(backend, remaining_second.reservation_id)
            elif first_reservation is None and second_reservation is not None:
                resources.release(backend, second_reservation.reservation_id)

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

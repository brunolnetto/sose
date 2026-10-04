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


def _fork_pair(philosopher: int, participants: int) -> tuple[int, int]:
    return tuple(sorted((philosopher, (philosopher + 1) % participants)))


def _philosopher_request_ids(
    philosopher: int,
    participants: int,
) -> tuple[str, str, int, int]:
    first, second = _fork_pair(philosopher, participants)
    first_id = f"p{philosopher}-fork-{first}"
    second_id = f"p{philosopher}-fork-{second}"
    return first_id, second_id, first, second


def _active_candidate(resources, participants: int) -> str:
    for philosopher in range(participants):
        first_id, second_id, _, _ = _philosopher_request_ids(
            philosopher,
            participants,
        )
        if (
            resources.reservation_for(first_id) is not None
            or resources.reservation_for(second_id) is not None
        ):
            return f"active:p{philosopher}"
    return "active:noop"


def _release_philosopher_if_holding(
    *,
    resources,
    backend,
    philosopher: int,
    participants: int,
    requested_at,
) -> None:
    first_id, second_id, _, second = _philosopher_request_ids(philosopher, participants)
    first_reservation = resources.reservation_for(first_id)
    second_reservation = resources.reservation_for(second_id)
    if first_reservation is not None and not resources.has_request(second_id):
        resources.ensure_requested(
            backend,
            resource_name=f"fork-{second}",
            request_id=second_id,
            requested_at=requested_at,
        )
        second_reservation = resources.reservation_for(second_id)

    if first_reservation is not None and second_reservation is not None:
        resources.release(backend, first_reservation.reservation_id)
        remaining_second = resources.reservation_for(second_id)
        if remaining_second is not None:
            resources.release(backend, remaining_second.reservation_id)
    elif first_reservation is None and second_reservation is not None:
        resources.release(backend, second_reservation.reservation_id)


def _candidate_for_state(resources, *, current, participants: int) -> str:
    if current.state == "ready":
        return "ready"
    if current.state == "active":
        return _active_candidate(resources, participants)
    return "noop"


def _reconcile_ready_action(
    engine,
    backend,
    *,
    current,
    resources,
    participants: int,
) -> bool:
    if current.state != "ready":
        return False
    for philosopher in range(participants):
        _, _, first, _ = _philosopher_request_ids(
            philosopher,
            participants,
        )
        resources.ensure_requested(
            backend,
            resource_name=f"fork-{first}",
            request_id=f"p{philosopher}-fork-{first}",
            requested_at=engine.context.clock.now,
        )
    transition(engine, current, "advance")
    return True


def _reconcile_active_action(
    persistence,
    engine,
    backend,
    *,
    current,
    action: str,
    resources,
    participants: int,
) -> None:
    if current.state != "active":
        return
    target = action.removeprefix("active:")
    if target != "noop":
        philosopher = int(target[1:])
        _release_philosopher_if_holding(
            resources=resources,
            backend=backend,
            philosopher=philosopher,
            participants=participants,
            requested_at=engine.context.clock.now,
        )

    if not persistence.resource_demands() and not persistence.resource_reservations():
        transition(engine, current, "finish")


def reconcile(persistence, engine, backend, config, case):
    if not config.enabled:
        return
    current = persistence.entity("canonical_case", case.id)
    resources = engine.resources

    candidate = _candidate_for_state(
        resources,
        current=current,
        participants=config.participants,
    )

    action = resolve_tick_action(
        persistence,
        backend,
        canonical="dining_philosophers",
        logical_tick=engine.context.clock.tick,
        candidate=candidate,
        requested_at=engine.context.clock.now,
    )

    if action == "ready":
        _reconcile_ready_action(
            engine,
            backend,
            current=current,
            resources=resources,
            participants=config.participants,
        )
        return

    if action.startswith("active:"):
        _reconcile_active_action(
            persistence,
            engine,
            backend,
            current=current,
            action=action,
            resources=resources,
            participants=config.participants,
        )


definition = DomainDefinition(
    name="dining_philosophers",
    kind="canonical",
    description="Canonical dining-philosophers problem demonstrating ordered durable Resource acquisition and contention.",
    config_model=DiningPhilosophersConfig, build_runtime=build_runtime, seed=seed,
    reconcile_tick=reconcile,
    runtime_mutable_fields=frozenset({"tick_step", "random_seed", "enabled"}),
)

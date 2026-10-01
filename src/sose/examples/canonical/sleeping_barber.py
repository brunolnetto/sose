from __future__ import annotations

from sose.core.resources import DurableResourceManager
from sose.core.runtime import ResourceDefinition, StoreDefinition
from sose.domain.config import DomainDefinition
from sose.core.stores import DurableStoreManager
from pydantic import Field

from .common import CanonicalConfig, build_runtime, resolve_tick_action, seed_case, transition


class SleepingBarberConfig(CanonicalConfig):
    participants: int = 5
    capacity: int = 1
    waiting_chairs: int = Field(default=3, ge=0)


def seed(persistence, config):
    if not persistence.resource_definitions():
        with persistence.transaction() as uow:
            uow.save_resource_definition(ResourceDefinition("barber", config.capacity))
    stores = DurableStoreManager(persistence)
    if not any(d.name == "abandoned" for d in persistence.store_definitions()):
        stores.define(StoreDefinition("abandoned"))
    return seed_case(persistence, name="sleeping_barber",
                     attributes={"customers": config.participants, "waiting_chairs": config.waiting_chairs})


def reconcile(persistence, engine, backend, config, case):
    if not config.enabled:
        return
    current = persistence.entity("canonical_case", case.id)
    resources = engine.resources

    if current.state == "ready":
        candidate = "ready"
    elif current.state == "active":
        reservations = sorted(
            persistence.resource_reservations(),
            key=lambda reservation: (reservation.sequence, reservation.request_id),
        )
        target = reservations[0].request_id if reservations else "noop"
        candidate = f"active:{target}"
    else:
        candidate = "noop"

    action = resolve_tick_action(
        persistence,
        backend,
        canonical="sleeping_barber",
        logical_tick=engine.context.clock.tick,
        candidate=candidate,
        requested_at=engine.context.clock.now,
    )

    if action == "ready":
        if current.state != "ready":
            return
        accepted = min(config.participants, config.waiting_chairs + config.capacity)
        stores = DurableStoreManager(persistence)
        for index in range(accepted, config.participants):
            stores.ensure_put(
                backend,
                store_name="abandoned",
                item_id=f"customer-{index}",
                value={"customer": index},
                requested_at=engine.context.clock.now,
            )
        for index in range(accepted):
            resources.ensure_requested(
                backend,
                resource_name="barber",
                request_id=f"customer-{index}",
                requested_at=engine.context.clock.now,
            )
        transition(engine, current, "advance")
        return

    if action.startswith("active:"):
        if current.state != "active":
            return
        target = action.removeprefix("active:")
        if target != "noop":
            reservation = resources.reservation_for(target)
            if reservation is not None:
                resources.release(backend, reservation.reservation_id)
        if not persistence.resource_demands() and not persistence.resource_reservations():
            transition(engine, current, "finish")


definition = DomainDefinition(
    name="sleeping_barber",
    kind="canonical",
    description="Canonical sleeping-barber queue demonstrating capacity, waiting-room admission, and durable Resource ownership.",
    config_model=SleepingBarberConfig, build_runtime=build_runtime, seed=seed,
    reconcile_tick=reconcile,
    runtime_mutable_fields=frozenset({"tick_step", "random_seed", "enabled"}),
)

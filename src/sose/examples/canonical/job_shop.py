from __future__ import annotations

from sose.core.resources import DurableResourceManager
from sose.core.runtime import ResourceDefinition, StoreDefinition
from sose.domain.config import DomainDefinition
from sose.core.stores import DurableStoreManager
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
    stores = DurableStoreManager(persistence)
    if not any(d.name == "completed_operations" for d in persistence.store_definitions()):
        stores.define(StoreDefinition("completed_operations"))
    return seed_case(persistence, name="job_shop",
                     attributes={"jobs": config.participants, "machines": config.machines})


def reconcile(persistence, engine, backend, config, case):
    if not config.enabled:
        return
    current = persistence.entity("canonical_case", case.id)
    resources = engine.resources
    stores = DurableStoreManager(persistence)

    def route(job):
        return (job % config.machines, (job + 1) % config.machines)

    completed = {
        item.item_id
        for item in persistence.store_items()
        if item.store_name == "completed_operations"
    }
    reservations = {r.request_id: r for r in persistence.resource_reservations()}

    if current.state == "ready":
        for job in range(config.participants):
            machine = route(job)[0]
            resources.ensure_requested(
                backend, resource_name=f"machine-{machine}",
                request_id=f"job-{job}-op-0",
                requested_at=engine.context.clock.now,
            )
        transition(engine, current, "advance")
        return

    if current.state != "active":
        return

    # Completing an owned operation creates a durable precedence marker.
    for job in range(config.participants):
        for operation in (0, 1):
            request_id = f"job-{job}-op-{operation}"
            marker = f"{request_id}-done"
            reservation = reservations.get(request_id)
            if reservation is not None and marker not in completed:
                resources.release(backend, reservation.reservation_id)
                stores.put(
                    backend,
                    store_name="completed_operations",
                    item_id=marker,
                    value={"job": job, "operation": operation},
                    requested_at=engine.context.clock.now,
                )
                return

    # Operation 1 becomes eligible only after operation 0's durable marker.
    for job in range(config.participants):
        predecessor = f"job-{job}-op-0-done"
        successor = f"job-{job}-op-1"
        successor_done = f"{successor}-done"
        if predecessor in completed and successor_done not in completed:
            if successor not in {d.request_id for d in persistence.resource_demands()}:
                machine = route(job)[1]
                resources.ensure_requested(
                    backend, resource_name=f"machine-{machine}",
                    request_id=successor,
                    requested_at=engine.context.clock.now,
                )

    completed = {
        item.item_id
        for item in persistence.store_items()
        if item.store_name == "completed_operations"
    }
    if all(f"job-{job}-op-1-done" in completed for job in range(config.participants)):
        transition(engine, persistence.entity("canonical_case", case.id), "finish")


definition = DomainDefinition(
    name="job_shop",
    kind="canonical",
    description="Canonical job-shop scheduling problem demonstrating deterministic multi-machine Resource contention.",
    config_model=JobShopConfig, build_runtime=build_runtime, seed=seed,
    reconcile_tick=reconcile,
    runtime_mutable_fields=frozenset({"tick_step", "random_seed", "enabled"}),
)

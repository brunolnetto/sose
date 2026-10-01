from __future__ import annotations

from sose.core.resources import DurableResourceManager
from sose.core.runtime import ResourceDefinition
from sose.domain.config import DomainDefinition
from pydantic import Field, model_validator

from .common import CanonicalConfig, build_runtime, seed_case, transition


class ReadersWritersConfig(CanonicalConfig):
    participants: int = 4
    readers: int = Field(default=3, ge=1)

    @model_validator(mode="after")
    def validate_readers(self):
        if self.readers >= self.participants:
            raise ValueError("readers must be less than participants so at least one writer exists")
        return self


def seed(persistence, config):
    if not persistence.resource_definitions():
        with persistence.transaction() as uow:
            uow.save_resource_definition(ResourceDefinition("reader_slots", config.readers))
            uow.save_resource_definition(ResourceDefinition("writer_gate", 1))
    return seed_case(persistence, name="readers_writers",
                     attributes={"readers": config.readers, "writers": config.participants - config.readers})


def reconcile(persistence, engine, backend, config, case):
    if not config.enabled:
        return
    current = persistence.entity("canonical_case", case.id)
    resources = engine.resources
    if current.state == "ready":
        # Readers may coexist. The writer gate is requested first by every
        # reader cohort and writer, making writer ownership exclusive while
        # reader_slots exposes concurrent read capacity.
        resources.ensure_requested(backend, resource_name="writer_gate",
            request_id="reader-cohort-gate", requested_at=engine.context.clock.now, priority=10)
        for index in range(config.readers):
            resources.ensure_requested(backend, resource_name="reader_slots",
                request_id=f"reader-{index}", requested_at=engine.context.clock.now, priority=10)
        for index in range(config.participants - config.readers):
            resources.ensure_requested(backend, resource_name="writer_gate",
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
    kind="canonical",
    description="Canonical readers/writers ownership problem demonstrating queued durable exclusion and priority.",
    config_model=ReadersWritersConfig, build_runtime=build_runtime, seed=seed,
    reconcile_tick=reconcile,
    runtime_mutable_fields=frozenset({"tick_step", "random_seed", "enabled"}),
)

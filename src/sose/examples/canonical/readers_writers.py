from __future__ import annotations

from sose.core.resources import DurableResourceManager
from sose.core.runtime import ResourceDefinition
from sose.domain.config import DomainDefinition
from pydantic import Field, model_validator

from .common import CanonicalConfig, build_runtime, resolve_tick_action, seed_case, transition


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


def _active_candidate(persistence) -> str:
    reservations = list(persistence.resource_reservations())
    reader_reservations = sorted(
        (
            reservation for reservation in reservations
            if reservation.resource_name == "reader_slots"
        ),
        key=lambda reservation: (reservation.sequence, reservation.request_id),
    )
    cohort_gate = next(
        (
            reservation for reservation in reservations
            if reservation.request_id == "reader-cohort-gate"
        ),
        None,
    )
    writer_reservations = sorted(
        (
            reservation for reservation in reservations
            if reservation.request_id.startswith("writer-")
        ),
        key=lambda reservation: (reservation.sequence, reservation.request_id),
    )
    if reader_reservations:
        target = reader_reservations[0].request_id
    elif cohort_gate is not None:
        target = cohort_gate.request_id
    elif writer_reservations:
        target = writer_reservations[0].request_id
    else:
        target = "noop"
    return f"active:{target}"


def _seed_reader_writer_demands(
    *,
    resources,
    backend,
    readers: int,
    writers: int,
    requested_at,
) -> None:
    resources.ensure_requested(
        backend,
        resource_name="writer_gate",
        request_id="reader-cohort-gate",
        requested_at=requested_at,
        priority=10,
    )
    for index in range(readers):
        resources.ensure_requested(
            backend,
            resource_name="reader_slots",
            request_id=f"reader-{index}",
            requested_at=requested_at,
            priority=10,
        )
    for index in range(writers):
        resources.ensure_requested(
            backend,
            resource_name="writer_gate",
            request_id=f"writer-{index}",
            requested_at=requested_at,
            priority=20,
        )


def _candidate_for_state(persistence, *, current) -> str:
    if current.state == "ready":
        return "ready"
    if current.state == "active":
        return _active_candidate(persistence)
    return "noop"


def _reconcile_ready_action(engine, backend, config, *, current, resources) -> bool:
    if current.state != "ready":
        return False
    _seed_reader_writer_demands(
        resources=resources,
        backend=backend,
        readers=config.readers,
        writers=config.participants - config.readers,
        requested_at=engine.context.clock.now,
    )
    return True


def _reconcile_active_action(persistence, engine, backend, *, current, action, resources) -> bool:
    if current.state != "active":
        return False
    target = action.removeprefix("active:")
    if target != "noop":
        reservation = resources.reservation_for(target)
        if reservation is not None:
            resources.release(backend, reservation.reservation_id)
    return not persistence.resource_demands() and not persistence.resource_reservations()


def reconcile(persistence, engine, backend, config, case):
    if not config.enabled:
        return
    current = persistence.entity("canonical_case", case.id)
    resources = engine.resources
    candidate = _candidate_for_state(persistence, current=current)

    action = resolve_tick_action(
        persistence,
        backend,
        canonical="readers_writers",
        logical_tick=engine.context.clock.tick,
        candidate=candidate,
        requested_at=engine.context.clock.now,
    )

    if action == "ready":
        if not _reconcile_ready_action(
            engine,
            backend,
            config,
            current=current,
            resources=resources,
        ):
            return
        transition(engine, current, "advance")
        return

    if action.startswith("active:"):
        if _reconcile_active_action(
            persistence,
            engine,
            backend,
            current=current,
            action=action,
            resources=resources,
        ):
            transition(engine, current, "finish")


definition = DomainDefinition(
    name="readers_writers",
    kind="canonical",
    description="Canonical readers/writers ownership problem demonstrating queued durable exclusion and priority.",
    config_model=ReadersWritersConfig, build_runtime=build_runtime, seed=seed,
    reconcile_tick=reconcile,
    runtime_mutable_fields=frozenset({"tick_step", "random_seed", "enabled"}),
)

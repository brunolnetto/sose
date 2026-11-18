from __future__ import annotations

from collections.abc import Mapping
from contextlib import nullcontext
from datetime import datetime, timedelta  # noqa: F401 - historical import location

from sose.backends.simpy import SimPyBackend
from sose.core.clock import SimulationClock  # noqa: F401 - historical import location
from sose.core.context import SimulationContext  # noqa: F401 - historical import location
from sose.core.engine import Engine
from sose.core.randomness import RandomSource  # noqa: F401 - historical import location
from sose.core.runtime import ResourceDefinition, StoreDefinition
from sose.core.resource_reservations import ResourceConflictError, TemporalReservation, authoritative_effect_reservation_id
from sose.core.scheduler import Scheduler  # noqa: F401 - historical import location
from sose.domain.registry import DomainRegistry, EntityType  # noqa: F401 - historical import location
from sose.persistence.memory import MemoryPersistence

from .entities import DeliveryAttempt, Shipment  # noqa: F401 - historical import location
from .statecharts import DeliveryAttemptChart, ShipmentChart  # noqa: F401 - historical import location
from .records import (  # noqa: F401 - historical import location
    ORIGIN,
    PICKUP_DUE,
    LogisticsEntities,
    _organization_queue,
    _shipment,
    build_runtime,
    delivery_attempt_id,
    flow_correlation_id,
)
from .queues import (  # noqa: F401 - historical import location
    _destination_queued,
    _ensure_origin_queue_dequeue,
)
from .delivery import (  # noqa: F401 - historical import location
    reconcile_delivery_dispatch,
    reconcile_delivery_failure,
    reconcile_delivery_success,
)


def _validated_resource_capacities(
    resource_capacities: Mapping[str, int] | None,
    resource_names: tuple[str, ...],
) -> dict[str, int]:
    capacities = {} if resource_capacities is None else dict(resource_capacities)
    if set(capacities) - set(resource_names):
        raise ValueError("unknown Logistics physical resource capacity override")
    if any(
        isinstance(capacity, bool)
        or not isinstance(capacity, int)
        or capacity < 1
        for capacity in capacities.values()
    ):
        raise ValueError("physical resource capacities must be positive integers")
    return capacities


def seed_reference(
    persistence: MemoryPersistence,
    *,
    owner_id: str,
    now: datetime = ORIGIN,
    service_level: str = "standard",
    route: str = "origin-a:destination-b",
    resource_capacity: int = 1,
    resource_capacities: Mapping[str, int] | None = None,
    hub_queue_capacity: int = 10,
    pickup_delay: timedelta = timedelta(hours=1),
    instance_key: str | None = None,
) -> LogisticsEntities:
    if instance_key is not None and (not isinstance(instance_key, str) or not instance_key.strip()):
        raise ValueError("instance_key must be a nonempty string")
    context, engine = build_runtime(persistence, owner_id=owner_id, now=now)
    shipment = context.entities.create(
        Shipment,
        key=(("logistics-reference", "shipment-1") if instance_key is None
             else ("logistics-reference", "shipment-1", instance_key)),
        state="created",
        attributes={"service_level": service_level, "route": route},
    )
    resource_names = (
        "pickup_courier", "origin_dock", "transfer_vehicle",
        "destination_dock", "delivery_courier",
    )
    capacities = _validated_resource_capacities(resource_capacities, resource_names)
    with persistence.transaction() as uow:
        uow.save_entity(shipment)
        for name in resource_names:
            uow.save_resource_definition(
                ResourceDefinition(
                    name, capacity=capacities.get(name, resource_capacity),
                )
            )

    engine.stores.define(StoreDefinition(_organization_queue(owner_id, "origin_hub_queue"), kind="fifo", capacity=hub_queue_capacity))
    engine.stores.define(
        StoreDefinition(_organization_queue(owner_id, "destination_hub_queue"), kind="fifo", capacity=hub_queue_capacity)
    )

    command = context.commands.create(
        "schedule_pickup",
        target=shipment,
        due_at=now + pickup_delay,
        correlation_id=flow_correlation_id(),
        key=("logistics-reference", shipment.id, "schedule-pickup"),
    )
    context.schedules.at(command.due_at, command=command)
    return LogisticsEntities(shipment_id=shipment.id)

def _dispatch(
    engine: Engine,
    entity,
    event: str,
    *,
    key: tuple[object, ...],
) -> None:
    command = engine.context.commands.create(
        event,
        target=entity,
        correlation_id=flow_correlation_id(),
        key=key,
    )
    engine.dispatch(command)


def reconcile_pickup(
    persistence: MemoryPersistence,
    engine: Engine,
    backend: SimPyBackend,
    *,
    entities: LogisticsEntities,
    authoritative_pickup: TemporalReservation | None = None,
    resource_persistence: MemoryPersistence | None = None,
    authoritative_effect_id: str | None = None,
) -> bool:
    shipment = _shipment(persistence, entities)
    courier_available = engine.context.scenarios.attribute(
        "logistics.courier.available", True
    )
    # Validate the resource before *any* business transition, including
    # resuming a delayed pickup. The domain UoW then commits under the same
    # pool-scoped PostgreSQL transaction as the resource validation.
    authorized, guard = _authoritative_pickup_guard(
        persistence, backend, shipment, authoritative_pickup, resource_persistence,
    )
    with guard:
        if authorized:
            _verify_authoritative_pickup(
                persistence, shipment, authoritative_pickup,
                authoritative_effect_id, scoped=resource_persistence is not None,
            )
        if shipment.state == "delayed_pickup" and courier_available:
            _dispatch(engine, shipment, "resume",
                      key=("logistics-pickup", shipment.id, "capacity-resume"))
            shipment = _shipment(persistence, entities)
        if shipment.state != "pickup_scheduled":
            return shipment.state not in {"created", "delayed_pickup"}
        if not courier_available:
            _dispatch(engine, shipment, "delay",
                      key=("logistics-pickup", shipment.id, "capacity-delay"))
            return False
        if authorized:
            # PostgreSQL owns physical capacity; never allocate this courier
            # again through the independent SimPy/Engine resource lifecycle.
            _dispatch(engine, shipment, "pickup", key=("logistics-pickup", shipment.id, "pickup"))
            return True
        return _pickup_with_local_courier(persistence, engine, backend, entities, shipment)


def _authoritative_pickup_guard(
    persistence: MemoryPersistence,
    backend: SimPyBackend,
    shipment,
    authoritative_pickup: TemporalReservation | None,
    resource_persistence: MemoryPersistence | None,
):
    authorized = authoritative_pickup is not None and shipment.state in {
        "pickup_scheduled", "delayed_pickup",
    }
    if authorized and authoritative_pickup.address.resource_type != "pickup_courier":
        raise ResourceConflictError("authoritative pickup requires pickup_courier")
    guard = (
        (resource_persistence or persistence).temporal_resources().authorize_use(
            authoritative_pickup, at=backend.now,
        )
        if authorized else nullcontext()
    )
    return authorized, guard


def _verify_authoritative_pickup(
    persistence: MemoryPersistence, shipment, pickup: TemporalReservation,
    authoritative_effect_id: str | None, *, scoped: bool,
) -> None:
    effect_id = authoritative_effect_id or pickup.reservation_id
    expected_resource_id = authoritative_effect_reservation_id(
        effect_id, scope=persistence.namespace if scoped else None,
    )
    if pickup.reservation_id != expected_resource_id:
        raise ResourceConflictError("authoritative pickup physical identity mismatch")
    with persistence.transaction() as uow:
        command = uow.get_command(effect_id)
        if (
            command is None
            or command.name != "composition.deliver_shipment"
            or command.entity_type != "shipment"
            or command.entity_id != shipment.id
            or command.causation_id != pickup.causation_id
            or not command.correlation_id
        ):
            raise ResourceConflictError(
                "authoritative pickup target or causal predecessor mismatch"
            )


def _pickup_with_local_courier(persistence, engine, backend, entities, shipment) -> bool:
    request_id = f"pickup-courier:{shipment.id}"
    reservation = engine.resources.ensure_requested(
        backend, resource_name="pickup_courier", request_id=request_id,
        requested_at=backend.now,
    )
    if reservation is None:
        return False
    shipment = _shipment(persistence, entities)
    _dispatch(engine, shipment, "pickup", key=("logistics-pickup", shipment.id, "pickup"))
    engine.resources.withdraw(backend, request_id)
    return True


def reconcile_origin_hub(
    persistence: MemoryPersistence,
    engine: Engine,
    backend: SimPyBackend,
    *,
    entities: LogisticsEntities,
) -> bool:
    shipment = _shipment(persistence, entities)
    if shipment.state == "at_origin_hub":
        return True
    if shipment.state != "picked_up":
        return False

    request_id = f"origin-dock:{shipment.id}"
    reservation = engine.resources.ensure_requested(
        backend,
        resource_name="origin_dock",
        request_id=request_id,
        requested_at=backend.now,
    )
    if reservation is None:
        return False

    item_id = f"origin-queue:{shipment.id}"
    if not any(item.item_id == item_id for item in persistence.store_items()):
        engine.stores.put(
            backend,
            store_name=_organization_queue(engine.owner_id, "origin_hub_queue"),
            item_id=item_id,
            value={"shipment_id": shipment.id},
            requested_at=backend.now,
        )
        backend.run_until(backend.now)

    if not any(item.item_id == item_id for item in persistence.store_items()):
        return False

    shipment = _shipment(persistence, entities)
    _dispatch(
        engine,
        shipment,
        "arrive_origin_hub",
        key=("logistics-origin", shipment.id, "arrive"),
    )
    engine.resources.withdraw(backend, request_id)
    return True

def _dispatch_transfer(persistence, engine, backend, entities, shipment, transfer_request) -> bool:
    vehicle = engine.resources.ensure_requested(
        backend, resource_name="transfer_vehicle", request_id=transfer_request,
        requested_at=backend.now,
    )
    if vehicle is None:
        return False
    if not _ensure_origin_queue_dequeue(persistence, engine, backend, shipment_id=shipment.id):
        return False
    shipment = _shipment(persistence, entities)
    _dispatch(engine, shipment, "dispatch_transfer",
              key=("logistics-transfer", shipment.id, "dispatch"))
    return True

def reconcile_transfer(
    persistence: MemoryPersistence,
    engine: Engine,
    backend: SimPyBackend,
    *,
    entities: LogisticsEntities,
) -> bool:
    shipment = _shipment(persistence, entities)
    transfer_request = f"transfer-vehicle:{shipment.id}"

    if shipment.state == "at_origin_hub" and not _dispatch_transfer(
        persistence, engine, backend, entities, shipment, transfer_request,
    ):
        return False

    shipment = _shipment(persistence, entities)
    if shipment.state != "in_transfer":
        return shipment.state == "at_destination_hub"

    dock_request = f"destination-dock:{shipment.id}"
    dock = engine.resources.ensure_requested(
        backend,
        resource_name="destination_dock",
        request_id=dock_request,
        requested_at=backend.now,
    )
    if dock is None:
        return False

    if not _destination_queued(persistence, engine, backend, shipment):
        return False

    shipment = _shipment(persistence, entities)
    _dispatch(
        engine,
        shipment,
        "arrive_destination_hub",
        key=("logistics-transfer", shipment.id, "arrive-destination"),
    )
    engine.resources.withdraw(backend, dock_request)
    engine.resources.withdraw(backend, transfer_request)
    return True

def run_happy_path(*, owner_id: str) -> tuple[MemoryPersistence, LogisticsEntities]:
    persistence = MemoryPersistence()
    entities = seed_reference(persistence, owner_id=owner_id)
    _, engine = build_runtime(persistence, owner_id=owner_id)
    backend = SimPyBackend(origin=ORIGIN)
    engine.rebuild_backend(backend)

    backend.run_until(PICKUP_DUE)
    if not reconcile_pickup(persistence, engine, backend, entities=entities):
        raise RuntimeError("pickup capacity unavailable")
    if not reconcile_origin_hub(persistence, engine, backend, entities=entities):
        raise RuntimeError("origin hub unavailable")
    if not reconcile_transfer(persistence, engine, backend, entities=entities):
        raise RuntimeError("transfer capacity unavailable")
    if not reconcile_delivery_dispatch(
        persistence, engine, backend, entities=entities, ordinal=1
    ):
        raise RuntimeError("delivery capacity unavailable")
    reconcile_delivery_success(
        persistence, engine, backend, entities=entities, ordinal=1
    )
    return persistence, entities


def run_failed_retry_path(*, owner_id: str) -> tuple[MemoryPersistence, LogisticsEntities]:
    persistence = MemoryPersistence()
    entities = seed_reference(persistence, owner_id=owner_id)
    _, engine = build_runtime(persistence, owner_id=owner_id)
    backend = SimPyBackend(origin=ORIGIN)
    engine.rebuild_backend(backend)

    backend.run_until(PICKUP_DUE)
    reconcile_pickup(persistence, engine, backend, entities=entities)
    reconcile_origin_hub(persistence, engine, backend, entities=entities)
    reconcile_transfer(persistence, engine, backend, entities=entities)
    reconcile_delivery_dispatch(
        persistence, engine, backend, entities=entities, ordinal=1
    )
    retry_at = reconcile_delivery_failure(
        persistence, engine, backend, entities=entities, ordinal=1
    )
    backend.run_until(retry_at)
    reconcile_delivery_dispatch(
        persistence, engine, backend, entities=entities, ordinal=2
    )
    reconcile_delivery_success(
        persistence, engine, backend, entities=entities, ordinal=2
    )
    return persistence, entities


# Moved by the quality-ratchet splits (#632); importing these names from here
# is deprecated (ADR-0011). Import them from the module named on the right.
from sose._deprecation import deprecated_reexports as _deprecated_reexports

__getattr__ = _deprecated_reexports(__name__, {
    "deterministic_id": "sose.core.identity",
    "ensure_delivery_attempt": "sose.examples.logistics.delivery",
}, since="0.8.1", removed_in="0.9.0")

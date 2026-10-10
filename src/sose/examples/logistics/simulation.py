from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sose.backends.simpy import SimPyBackend
from sose.core.clock import SimulationClock
from sose.core.context import SimulationContext
from sose.core.engine import Engine
from sose.core.identity import deterministic_id
from sose.core.randomness import RandomSource
from sose.core.runtime import ResourceDefinition, StoreDefinition
from sose.core.resource_reservations import ResourceConflictError, TemporalReservation
from sose.core.scheduler import Scheduler
from sose.domain.registry import DomainRegistry, EntityType
from sose.persistence.memory import MemoryPersistence

from .entities import DeliveryAttempt, Shipment
from .statecharts import DeliveryAttemptChart, ShipmentChart


ORIGIN = datetime(2026, 3, 1, 8, tzinfo=timezone.utc)
PICKUP_DUE = ORIGIN + timedelta(hours=1)
RETRY_DELAY = timedelta(hours=2)


@dataclass(frozen=True, slots=True)
class LogisticsEntities:
    shipment_id: str


def flow_correlation_id() -> str:
    return deterministic_id("logistics-flow", "reference", "shipment-1")


def delivery_attempt_id(ordinal: int, *, shipment_id: str | None = None) -> str:
    if ordinal < 1:
        raise ValueError("delivery attempt ordinal must be >= 1")
    legacy_shipment = deterministic_id("entity", "shipment", "logistics-reference", "shipment-1")
    if shipment_id is not None and not shipment_id:
        raise ValueError("shipment_id must be nonempty")
    identity = "shipment-1" if shipment_id in (None, legacy_shipment) else shipment_id
    return deterministic_id(
        "entity", "delivery_attempt", "logistics-reference", identity, "attempt", ordinal,
    )


def build_runtime(
    persistence: MemoryPersistence,
    *,
    now: datetime = ORIGIN,
    tick: int = 0,
    scenarios=(),
    step: timedelta = timedelta(hours=1),
    random_seed: int = 126,
) -> tuple[SimulationContext, Engine]:
    context = SimulationContext(
        clock=SimulationClock(now=now, step=step, tick=tick),
        random=RandomSource(root_seed=random_seed),
        scheduler=Scheduler(),
    )
    registry = DomainRegistry()
    registry.register(EntityType("shipment", ShipmentChart))
    registry.register(EntityType("delivery_attempt", DeliveryAttemptChart))
    return context, Engine(
        context=context,
        registry=registry,
        persistence=persistence,
        scenarios=scenarios,
    )


def seed_reference(
    persistence: MemoryPersistence,
    *,
    now: datetime = ORIGIN,
    service_level: str = "standard",
    route: str = "origin-a:destination-b",
    resource_capacity: int = 1,
    hub_queue_capacity: int = 10,
    pickup_delay: timedelta = timedelta(hours=1),
    instance_key: str | None = None,
) -> LogisticsEntities:
    if instance_key is not None and (not isinstance(instance_key, str) or not instance_key.strip()):
        raise ValueError("instance_key must be a nonempty string")
    context, engine = build_runtime(persistence, now=now)
    shipment = context.entities.create(
        Shipment,
        key=(("logistics-reference", "shipment-1") if instance_key is None
             else ("logistics-reference", "shipment-1", instance_key)),
        state="created",
        attributes={"service_level": service_level, "route": route},
    )
    with persistence.transaction() as uow:
        uow.save_entity(shipment)
        for name in (
            "pickup_courier",
            "origin_dock",
            "transfer_vehicle",
            "destination_dock",
            "delivery_courier",
        ):
            uow.save_resource_definition(ResourceDefinition(name, capacity=resource_capacity))

    engine.stores.define(StoreDefinition("origin_hub_queue", kind="fifo", capacity=hub_queue_capacity))
    engine.stores.define(
        StoreDefinition("destination_hub_queue", kind="fifo", capacity=hub_queue_capacity)
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


def _shipment(persistence: MemoryPersistence, entities: LogisticsEntities) -> Shipment:
    shipment = persistence.entity("shipment", entities.shipment_id)
    if shipment is None:
        raise RuntimeError("shipment was not persisted")
    return shipment


def _attempt(
    persistence: MemoryPersistence, ordinal: int, *, shipment_id: str | None = None,
) -> DeliveryAttempt | None:
    return persistence.entity(
        "delivery_attempt", delivery_attempt_id(ordinal, shipment_id=shipment_id),
    )


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
) -> bool:
    shipment = _shipment(persistence, entities)
    courier_available = engine.context.scenarios.attribute(
        "logistics.courier.available", True
    )
    if shipment.state == "delayed_pickup" and courier_available:
        _dispatch(
            engine,
            shipment,
            "resume",
            key=("logistics-pickup", shipment.id, "capacity-resume"),
        )
        shipment = _shipment(persistence, entities)

    if shipment.state != "pickup_scheduled":
        return shipment.state not in {"created", "delayed_pickup"}

    if not courier_available:
        _dispatch(
            engine,
            shipment,
            "delay",
            key=("logistics-pickup", shipment.id, "capacity-delay"),
        )
        return False

    if authoritative_pickup is not None:
        # The same PostgreSQL transaction pins physical capacity while the
        # nested Engine UoW commits the pickup. SimPy never allocates a second
        # courier for this opt-in path.
        if authoritative_pickup.address.resource_type != "pickup_courier":
            raise ResourceConflictError("authoritative pickup requires pickup_courier")
        with persistence.temporal_resources().authorize_use(
            authoritative_pickup, at=backend.now,
        ):
            with persistence.transaction() as uow:
                command = uow.get_command(authoritative_pickup.reservation_id)
                if (
                    command is None
                    or command.name != "composition.deliver_shipment"
                    or command.entity_type != "shipment"
                    or command.entity_id != shipment.id
                    or command.causation_id != authoritative_pickup.causation_id
                    or not command.correlation_id
                ):
                    raise ResourceConflictError(
                        "authoritative pickup target or causal predecessor mismatch"
                    )
            _dispatch(
                engine, shipment, "pickup",
                key=("logistics-pickup", shipment.id, "pickup"),
            )
        return True

    request_id = f"pickup-courier:{shipment.id}"
    reservation = engine.resources.ensure_requested(
        backend,
        resource_name="pickup_courier",
        request_id=request_id,
        requested_at=backend.now,
    )
    if reservation is None:
        return False

    shipment = _shipment(persistence, entities)
    _dispatch(
        engine,
        shipment,
        "pickup",
        key=("logistics-pickup", shipment.id, "pickup"),
    )
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
            store_name="origin_hub_queue",
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


def _has_store_get_result(persistence: MemoryPersistence, request_id: str) -> bool:
    return any(
        result.request_id == request_id
        for result in persistence.store_get_results()
    )


def _has_store_get_request(persistence: MemoryPersistence, request_id: str) -> bool:
    return any(
        request.request_id == request_id
        for request in persistence.store_get_requests()
    )


def _ensure_origin_queue_dequeue(
    persistence: MemoryPersistence,
    engine: Engine,
    backend: SimPyBackend,
    *,
    shipment_id: str,
) -> bool:
    dequeue_id = f"origin-dequeue:{shipment_id}"
    if _has_store_get_result(persistence, dequeue_id):
        return True
    if not _has_store_get_request(persistence, dequeue_id):
        engine.stores.get(
            backend,
            store_name="origin_hub_queue",
            request_id=dequeue_id,
            requested_at=backend.now,
        )
    backend.run_until(backend.now)
    return _has_store_get_result(persistence, dequeue_id)


def _ensure_destination_queue_dequeue(
    persistence: MemoryPersistence,
    engine: Engine,
    backend: SimPyBackend,
    *,
    shipment_id: str,
) -> bool:
    dequeue_id = f"destination-dequeue:{shipment_id}"
    if _has_store_get_result(persistence, dequeue_id):
        return True
    if not _has_store_get_request(persistence, dequeue_id):
        engine.stores.get(
            backend,
            store_name="destination_hub_queue",
            request_id=dequeue_id,
            requested_at=backend.now,
        )
    backend.run_until(backend.now)
    return _has_store_get_result(persistence, dequeue_id)


def reconcile_transfer(
    persistence: MemoryPersistence,
    engine: Engine,
    backend: SimPyBackend,
    *,
    entities: LogisticsEntities,
) -> bool:
    shipment = _shipment(persistence, entities)
    transfer_request = f"transfer-vehicle:{shipment.id}"

    if shipment.state == "at_origin_hub":
        vehicle = engine.resources.ensure_requested(
            backend,
            resource_name="transfer_vehicle",
            request_id=transfer_request,
            requested_at=backend.now,
        )
        if vehicle is None:
            return False
        if not _ensure_origin_queue_dequeue(
            persistence,
            engine,
            backend,
            shipment_id=shipment.id,
        ):
            return False

        shipment = _shipment(persistence, entities)
        _dispatch(
            engine,
            shipment,
            "dispatch_transfer",
            key=("logistics-transfer", shipment.id, "dispatch"),
        )

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

    item_id = f"destination-queue:{shipment.id}"
    if not any(item.item_id == item_id for item in persistence.store_items()):
        engine.stores.put(
            backend,
            store_name="destination_hub_queue",
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
        "arrive_destination_hub",
        key=("logistics-transfer", shipment.id, "arrive-destination"),
    )
    engine.resources.withdraw(backend, dock_request)
    engine.resources.withdraw(backend, transfer_request)
    return True


def ensure_delivery_attempt(
    persistence: MemoryPersistence,
    engine: Engine,
    *,
    ordinal: int,
    shipment_id: str | None = None,
) -> DeliveryAttempt:
    legacy_shipment = deterministic_id(
        "entity", "shipment", "logistics-reference", "shipment-1",
    )
    actual_shipment = legacy_shipment if shipment_id is None else shipment_id
    existing = _attempt(persistence, ordinal, shipment_id=actual_shipment)
    if existing is not None:
        if existing.attributes.get("shipment_id") != actual_shipment:
            raise ValueError("delivery attempt belongs to another shipment")
        return existing

    attempt = engine.context.entities.create(
        DeliveryAttempt,
        key=("logistics-reference",
             "shipment-1" if actual_shipment == legacy_shipment else actual_shipment,
             "attempt", ordinal),
        state="pending",
        attributes={"shipment_id": actual_shipment, "ordinal": ordinal},
    )
    with persistence.transaction() as uow:
        uow.save_entity(attempt)
    return attempt


def _resume_destination_hub_if_available(
    persistence: MemoryPersistence,
    engine: Engine,
    *,
    entities: LogisticsEntities,
    ordinal: int,
    courier_available: bool,
):
    shipment = _shipment(persistence, entities)
    if shipment.state != "delayed_destination_hub" or not courier_available:
        return shipment
    _dispatch(
        engine,
        shipment,
        "resume",
        key=("logistics-delivery", shipment.id, ordinal, "capacity-resume"),
    )
    return _shipment(persistence, entities)


def _eligible_delivery_dispatch_state(shipment) -> bool:
    return shipment.state in {"at_destination_hub", "out_for_delivery"}


def _attempt_ready_for_dispatch(attempt: DeliveryAttempt) -> bool:
    if attempt.state == "out_for_delivery":
        return True
    return attempt.state == "pending"


def _dispatch_delay_if_unavailable(
    engine: Engine,
    *,
    shipment,
    ordinal: int,
    courier_available: bool,
) -> bool:
    if courier_available:
        return False
    if shipment.state == "at_destination_hub":
        _dispatch(
            engine,
            shipment,
            "delay",
            key=("logistics-delivery", shipment.id, ordinal, "capacity-delay"),
        )
    return True


def _dispatch_shipment_for_delivery(
    persistence: MemoryPersistence,
    engine: Engine,
    backend: SimPyBackend,
    *,
    entities: LogisticsEntities,
    shipment,
    ordinal: int,
) -> bool:
    if shipment.state != "at_destination_hub":
        return True
    if not _ensure_destination_queue_dequeue(
        persistence,
        engine,
        backend,
        shipment_id=shipment.id,
    ):
        return False
    shipment = _shipment(persistence, entities)
    _dispatch(
        engine,
        shipment,
        "dispatch_delivery",
        key=("logistics-delivery", shipment.id, ordinal, "dispatch-shipment"),
    )
    return True


def reconcile_delivery_dispatch(
    persistence: MemoryPersistence,
    engine: Engine,
    backend: SimPyBackend,
    *,
    entities: LogisticsEntities,
    ordinal: int,
) -> bool:
    courier_available = engine.context.scenarios.attribute(
        "logistics.courier.available", True
    )
    shipment = _resume_destination_hub_if_available(
        persistence,
        engine,
        entities=entities,
        ordinal=ordinal,
        courier_available=courier_available,
    )
    if not _eligible_delivery_dispatch_state(shipment):
        return False

    attempt = ensure_delivery_attempt(
        persistence, engine, ordinal=ordinal, shipment_id=entities.shipment_id,
    )
    if attempt.state == "out_for_delivery":
        return True
    if not _attempt_ready_for_dispatch(attempt):
        return False

    if _dispatch_delay_if_unavailable(
        engine,
        shipment=shipment,
        ordinal=ordinal,
        courier_available=courier_available,
    ):
        return False

    request_id = f"delivery-courier:{attempt.id}"
    if engine.resources.ensure_requested(
        backend,
        resource_name="delivery_courier",
        request_id=request_id,
        requested_at=backend.now,
    ) is None:
        return False

    if not _dispatch_shipment_for_delivery(
        persistence,
        engine,
        backend,
        entities=entities,
        shipment=shipment,
        ordinal=ordinal,
    ):
        return False

    attempt = _attempt(persistence, ordinal, shipment_id=entities.shipment_id)
    if attempt is None:
        raise RuntimeError("delivery attempt disappeared")
    _dispatch(
        engine,
        attempt,
        "dispatch",
        key=("logistics-delivery", attempt.id, "dispatch"),
    )
    return True


def reconcile_delivery_success(
    persistence: MemoryPersistence,
    engine: Engine,
    backend: SimPyBackend,
    *,
    entities: LogisticsEntities,
    ordinal: int,
) -> None:
    attempt = _attempt(persistence, ordinal, shipment_id=entities.shipment_id)
    shipment = _shipment(persistence, entities)
    if attempt is None or attempt.state != "out_for_delivery":
        raise RuntimeError("delivery attempt is not active")
    if shipment.state != "out_for_delivery":
        raise RuntimeError("shipment is not out for delivery")

    _dispatch(
        engine,
        attempt,
        "deliver",
        key=("logistics-delivery", attempt.id, "deliver"),
    )
    shipment = _shipment(persistence, entities)
    _dispatch(
        engine,
        shipment,
        "deliver",
        key=("logistics-delivery", shipment.id, ordinal, "deliver"),
    )
    engine.resources.withdraw(backend, f"delivery-courier:{attempt.id}")


def reconcile_delivery_failure(
    persistence: MemoryPersistence,
    engine: Engine,
    backend: SimPyBackend,
    *,
    entities: LogisticsEntities,
    ordinal: int,
    retry_after: timedelta = RETRY_DELAY,
) -> datetime:
    attempt = _attempt(persistence, ordinal, shipment_id=entities.shipment_id)
    shipment = _shipment(persistence, entities)
    if attempt is None or attempt.state != "out_for_delivery":
        raise RuntimeError("delivery attempt is not active")
    if shipment.state != "out_for_delivery":
        raise RuntimeError("shipment is not out for delivery")

    _dispatch(
        engine,
        attempt,
        "fail",
        key=("logistics-delivery", attempt.id, "fail"),
    )
    shipment = _shipment(persistence, entities)
    _dispatch(
        engine,
        shipment,
        "delay",
        key=("logistics-delivery", shipment.id, ordinal, "retry-delay"),
    )

    shipment = _shipment(persistence, entities)
    due_at = backend.now + retry_after
    command = engine.context.commands.create(
        "resume",
        target=shipment,
        due_at=due_at,
        correlation_id=flow_correlation_id(),
        key=("logistics-delivery", shipment.id, ordinal, "retry-resume"),
    )
    engine.context.schedules.at(due_at, command=command)
    engine.resources.withdraw(backend, f"delivery-courier:{attempt.id}")
    return due_at


def run_happy_path() -> tuple[MemoryPersistence, LogisticsEntities]:
    persistence = MemoryPersistence()
    entities = seed_reference(persistence)
    _, engine = build_runtime(persistence)
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


def run_failed_retry_path() -> tuple[MemoryPersistence, LogisticsEntities]:
    persistence = MemoryPersistence()
    entities = seed_reference(persistence)
    _, engine = build_runtime(persistence)
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

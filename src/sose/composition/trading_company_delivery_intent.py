"""Logistics shipment-delivery intent executor, split from trading_company_customer."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from sose.backends.simpy import SimPyBackend
from sose.core.events import Command
from sose.core.resource_reservations import ResourceConflictError
from sose.persistence.memory import MemoryPersistence

from sose.examples.logistics import simulation as logistics

from .trading_company_customer_support import (
    DurableResourceWait,
    _causal_command_scope,
    _IntentExecution,
    _raise_delivery_dispatch_failure_or_wait,
)


def _pickup_command_due_times(persistence: MemoryPersistence, shipment) -> list[datetime]:
    return [
        cmd.due_at
        for work in persistence.scheduled_work()
        if (cmd := persistence.command(work.command_id)) is not None
        and cmd.name == "schedule_pickup"
        and cmd.entity_type == "shipment"
        and cmd.entity_id == shipment.id
    ]


def pickup_resource_due_at(
    persistence: MemoryPersistence,
    intent: Command,
    *,
    organization_id: str,
) -> datetime:
    if not isinstance(organization_id, str) or not organization_id.strip():
        raise ValueError("organization_id must not be empty")
    """Resolve the earliest physical pickup time from durable domain state."""
    shipment = persistence.entity("shipment", intent.entity_id)
    if shipment is None:
        raise RuntimeError("pickup demand references a missing shipment")
    position = persistence.simulation_position(owner_id=organization_id)
    anchor = max(
        intent.due_at,
        position.logical_time if position is not None else intent.due_at,
    )
    if shipment.state == "created":
        return anchor + timedelta(hours=1)
    pickup_times = _pickup_command_due_times(persistence, shipment)
    return max(anchor, min(pickup_times)) if pickup_times else anchor


@dataclass(frozen=True, slots=True)
class _DeliveryRecovery:
    """One delivery intent together with the runtime rebuilt to resume it."""

    run: _IntentExecution
    engine: object
    backend: SimPyBackend
    shipment_id: str

    def current_state(self) -> str:
        current = self.run.persistence.entity("shipment", self.shipment_id)
        if current is None:
            raise RuntimeError("logistics shipment disappeared during recovery")
        return current.state


def _reset_and_schedule_pickup(
    run: _IntentExecution, engine, shipment, *, shipment_id: str, recovery_time: datetime,
) -> None:
    persistence, intent = run.persistence, run.intent
    authoritative_pickup = run.authoritative_pickup
    # Only an unstarted shipment may reset an inherited demo
    # pickup schedule. Existing progress is never erased.
    for work in persistence.scheduled_work():
        scheduled = persistence.command(work.command_id)
        if (
            scheduled is not None
            and scheduled.entity_type == "shipment"
            and scheduled.entity_id == shipment_id
            and scheduled.name == "schedule_pickup"
        ):
            with persistence.transaction() as uow:
                uow.delete_scheduled_work(work.work_id)
                uow.delete_command(scheduled.command_id)
    pickup_due = (
        authoritative_pickup.start_at if authoritative_pickup is not None
        else recovery_time + timedelta(hours=1)
    )
    if pickup_due < recovery_time:
        raise ResourceConflictError(
            "authoritative pickup interval precedes persisted recovery clock"
        )
    pickup = engine.context.commands.create(
        "schedule_pickup", target=shipment, due_at=pickup_due,
        key=("trading-company", shipment.id, "schedule-pickup", intent.command_id),
    )
    engine.context.schedules.at(pickup_due, command=pickup)


def _scheduled_pickup_times(persistence: MemoryPersistence, shipment_id: str) -> list[datetime]:
    return [
        scheduled.due_at
        for work in persistence.scheduled_work()
        if (scheduled := persistence.command(work.command_id)) is not None
        and scheduled.name == "schedule_pickup"
        and scheduled.entity_type == "shipment"
        and scheduled.entity_id == shipment_id
    ]


def _advance_to_pickup(recovery: _DeliveryRecovery) -> None:
    backend = recovery.backend
    authoritative_pickup = recovery.run.authoritative_pickup
    # For a resumed schedule, respect the existing durable due time
    # rather than scheduling a second pickup command.
    pickup_times = _scheduled_pickup_times(recovery.run.persistence, recovery.shipment_id)
    if pickup_times:
        pickup_due = max(backend.now, min(pickup_times))
        if authoritative_pickup is not None:
            pickup_due = max(pickup_due, authoritative_pickup.start_at)
        if pickup_due > backend.now:
            backend.run_until(pickup_due)
        else:
            backend.run_until(backend.now)
    elif authoritative_pickup is not None and authoritative_pickup.start_at > backend.now:
        backend.run_until(authoritative_pickup.start_at)


def _resume_pickup(recovery: _DeliveryRecovery) -> None:
    run, engine, backend = recovery.run, recovery.engine, recovery.backend
    authoritative_pickup = run.authoritative_pickup
    _advance_to_pickup(recovery)
    if not logistics.reconcile_pickup(
        run.persistence, engine, backend, entities=run.fixtures.logistics,
        authoritative_pickup=authoritative_pickup,
        resource_persistence=run.resource_persistence,
        authoritative_effect_id=run.effect_id,
    ):
        request_id = f"pickup-courier:{recovery.shipment_id}"
        if (
            authoritative_pickup is None
            and engine.resources.has_request(request_id)
        ):
            # A demand or grant is already durable; its status can
            # change between pickup's check and this read. Retry
            # the same identity rather than inventing a new
            # occurrence or
            # recording terminal business failure would be wrong.
            raise DurableResourceWait("pickup_courier", request_id)
        raise RuntimeError("logistics pickup failed")


def _resume_origin_hub(recovery: _DeliveryRecovery) -> None:
    run, engine, backend = recovery.run, recovery.engine, recovery.backend
    if not logistics.reconcile_origin_hub(
        run.persistence, engine, backend, entities=run.fixtures.logistics,
    ):
        request_id = f"origin-dock:{recovery.shipment_id}"
        if engine.resources.has_request(request_id):
            # Another organization's finite dock occupancy or a
            # pending durable hub-queue put is backpressure, not
            # a failed shipment. Keep its original Command and
            # resume against the persisted request on retry.
            raise DurableResourceWait("origin_dock", request_id)
        raise RuntimeError("logistics origin-hub handling failed")


def _raise_if_transfer_resource_waits(
    engine, waiting_resources: tuple, preexisting_grants: dict,
) -> None:
    for resource_name, request_id in waiting_resources:
        current_grant = engine.resources.reservation_for(request_id)
        if engine.resources.has_request(request_id) and (
            current_grant is None
            or preexisting_grants[request_id] is None
        ):
            raise DurableResourceWait(resource_name, request_id)


def _raise_if_queue_request_pending(
    persistence: MemoryPersistence,
    *,
    origin_queue: str,
    origin_request: str,
    destination_queue: str,
    destination_id: str,
) -> None:
    if any(
        request.request_id == origin_request
        and request.store_name == origin_queue
        for request in persistence.store_get_requests()
    ):
        raise DurableResourceWait(origin_queue, origin_request)
    if any(
        intent_record.item_id == destination_id
        and intent_record.store_name == destination_queue
        for intent_record in persistence.store_put_intents()
    ):
        raise DurableResourceWait(destination_queue, destination_id)


def _raise_if_origin_dequeue_committed(
    recovery: _DeliveryRecovery, *, origin_queue: str, origin_request: str,
) -> None:
    # A worker can commit a dequeue/put after the local
    # reconciliation returned False. Only exact matching
    # durable receipts justify retrying the original Command.
    if recovery.current_state() == "at_origin_hub":
        selection = recovery.engine.stores.selection(origin_request)
        if (
            selection is not None
            and selection.store_name == origin_queue
            and selection.item.item_id == f"origin-queue:{recovery.shipment_id}"
        ):
            raise DurableResourceWait(origin_queue, origin_request)


def _raise_if_destination_put_committed(
    recovery: _DeliveryRecovery, *, destination_queue: str, destination_id: str,
) -> None:
    persistence = recovery.run.persistence
    if recovery.current_state() == "in_transfer" and (
        any(
            item.item_id == destination_id
            and item.store_name == destination_queue
            for item in persistence.store_items()
        )
        or any(
            result.item.item_id == destination_id
            and result.store_name == destination_queue
            for result in persistence.store_get_results()
        )
    ):
        raise DurableResourceWait(destination_queue, destination_id)


def _resume_transfer(recovery: _DeliveryRecovery) -> None:
    run, engine, backend = recovery.run, recovery.engine, recovery.backend
    persistence, shipment_id = run.persistence, recovery.shipment_id
    # Resource identities are physical; queue identities remain
    # scoped to the owning organization.
    waiting_resources = (
        ("transfer_vehicle", f"transfer-vehicle:{shipment_id}"),
        ("destination_dock", f"destination-dock:{shipment_id}"),
    )
    preexisting_grants = {
        request_id: engine.resources.reservation_for(request_id)
        for _, request_id in waiting_resources
    }
    if not logistics.reconcile_transfer(
        persistence, engine, backend, entities=run.fixtures.logistics,
    ):
        _raise_if_transfer_resource_waits(engine, waiting_resources, preexisting_grants)

        origin_queue = logistics._organization_queue(
            run.organization_id, "origin_hub_queue",
        )
        destination_queue = logistics._organization_queue(
            run.organization_id, "destination_hub_queue",
        )
        origin_request = f"origin-dequeue:{shipment_id}"
        destination_id = f"destination-queue:{shipment_id}"
        _raise_if_queue_request_pending(
            persistence, origin_queue=origin_queue, origin_request=origin_request,
            destination_queue=destination_queue, destination_id=destination_id,
        )
        _raise_if_origin_dequeue_committed(
            recovery, origin_queue=origin_queue, origin_request=origin_request,
        )
        _raise_if_destination_put_committed(
            recovery, destination_queue=destination_queue, destination_id=destination_id,
        )
        # No exact durable evidence means a genuine domain failure.
        raise RuntimeError("logistics transfer failed")


def _resume_delivery_dispatch(recovery: _DeliveryRecovery) -> None:
    run = recovery.run
    if not logistics.reconcile_delivery_dispatch(
        run.persistence, recovery.engine, recovery.backend, entities=run.fixtures.logistics,
        ordinal=1,
    ):
        _raise_delivery_dispatch_failure_or_wait(
            run.persistence, shipment_id=recovery.shipment_id, ordinal=1,
        )


def _resume_out_for_delivery(recovery: _DeliveryRecovery) -> None:
    run, engine, backend = recovery.run, recovery.engine, recovery.backend
    persistence, shipment_id = run.persistence, recovery.shipment_id
    attempt = persistence.entity(
        "delivery_attempt", logistics.delivery_attempt_id(1, shipment_id=shipment_id),
    )
    if attempt is not None and attempt.state == "delivered":
        # Crash after attempt.deliver but before shipment.deliver:
        # do not dispatch the already-committed attempt twice.
        active = persistence.entity("shipment", shipment_id)
        logistics._dispatch(
            engine, active, "deliver",
            key=("logistics-delivery", shipment_id, 1, "deliver"),
        )
        engine.resources.withdraw(
            backend, f"delivery-courier:{attempt.id}",
        )
    else:
        logistics.reconcile_delivery_success(
            persistence, engine, backend,
            entities=run.fixtures.logistics, ordinal=1,
        )


def _resume_delivery_stages(recovery: _DeliveryRecovery) -> None:
    if recovery.current_state() in {"created", "pickup_scheduled", "delayed_pickup"}:
        _resume_pickup(recovery)

    if recovery.current_state() == "picked_up":
        _resume_origin_hub(recovery)

    if recovery.current_state() in {"at_origin_hub", "in_transfer"}:
        _resume_transfer(recovery)

    if recovery.current_state() in {"at_destination_hub", "delayed_destination_hub"}:
        _resume_delivery_dispatch(recovery)

    if recovery.current_state() == "out_for_delivery":
        _resume_out_for_delivery(recovery)

    if recovery.current_state() != "delivered":
        raise RuntimeError(
            "logistics delivery recovery did not reach committed delivered state"
        )


def _execute_deliver_shipment(run: _IntentExecution) -> None:
    persistence, intent, organization_id = run.persistence, run.intent, run.organization_id
    # A worker can die after *any* statechart transition. Restart from
    # durable shipment state rather than replaying the initial schedule.
    shipment_id = run.fixtures.logistics.shipment_id
    shipment = persistence.entity("shipment", shipment_id)
    if shipment is None:
        raise RuntimeError("logistics shipment disappeared")
    position = persistence.simulation_position(owner_id=organization_id)
    restore_at = position.logical_time if position is not None else intent.due_at
    recovery_time = max(restore_at, intent.due_at)
    _, engine = logistics.build_runtime(persistence, owner_id=organization_id, now=restore_at)
    backend = SimPyBackend(origin=restore_at)

    with _causal_command_scope(
        engine, intent=intent, correlation_id=run.correlation_id,
    ):
        if shipment.state == "created":
            _reset_and_schedule_pickup(
                run, engine, shipment, shipment_id=shipment_id, recovery_time=recovery_time,
            )
        engine.rebuild_backend(backend)
        # Rebuild at the *authoritative* persisted position, never at an
        # arbitrarily later inbound message timestamp. Then advance to
        # the requested command time without violating durable lineage.
        # Materialize reattached resource leases before running forward.
        backend.run_until(backend.now)
        if recovery_time > backend.now:
            backend.run_until(recovery_time)
        engine.context.clock.now = recovery_time
        _resume_delivery_stages(
            _DeliveryRecovery(run=run, engine=engine, backend=backend, shipment_id=shipment_id),
        )

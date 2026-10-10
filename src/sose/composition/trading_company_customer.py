from __future__ import annotations

from contextlib import contextmanager, nullcontext
from dataclasses import dataclass
from datetime import datetime, timedelta
from math import isfinite

from sose.backends.simpy import SimPyBackend
from sose.core.events import Command
from sose.core.identity import deterministic_id
from sose.persistence.memory import MemoryPersistence

from sose.examples.order_to_cash import simulation as o2c
from sose.examples.warehouse_fulfillment import simulation as fulfillment
from sose.examples.logistics import simulation as logistics
from sose.examples.cards_payments import simulation as payments
from sose.examples.record_to_report import simulation as r2r
from sose.examples.warehouse_management import simulation as wm

from .boundary import BoundaryConsumerRegistry, BoundaryService
from .effects import BusinessEffectService, CERTIFIED_INTENTS
from .bindings import CustomerSettlementBinding, SettlementBindingService
from .model import BoundaryMessage, DeliveryStatus


@dataclass(frozen=True, slots=True)
class CustomerDemandPathResult:
    persistence: MemoryPersistence
    correlation_id: str
    o2c_order_id: str
    fulfillment_order_id: str
    shipment_id: str
    payment_id: str
    journal_id: str
    warehouse_stock_id: str
    reservation_reference: str
    consumption_reference: str
    message_ids: tuple[str, ...]
    effect_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class _CustomerFixtures:
    o2c: o2c.O2CEntities
    fulfillment: fulfillment.WarehouseEntities
    warehouse: wm.WarehouseManagementEntities
    logistics: logistics.LogisticsEntities
    payments: payments.PaymentEntities
    r2r: r2r.R2REntities


def _intent_handler(
    *,
    intent_name: str,
    entity_type: str,
    entity_id: str,
):
    def handle(message: BoundaryMessage, uow) -> str:
        command_id = deterministic_id(
            "composition-domain-intent",
            message.message_id,
            intent_name,
            entity_type,
            entity_id,
        )
        command = Command(
            command_id=command_id,
            name=intent_name,
            entity_type=entity_type,
            entity_id=entity_id,
            due_at=message.produced_at,
            issued_at=message.produced_at,
            payload={
                **message.payload(),
                "boundary_message_id": message.message_id,
                "boundary_contract": message.contract_key,
            },
            causation_id=message.message_id,
            correlation_id=message.correlation_id,
        )
        existing = uow.get_command(command_id)
        if existing is not None and existing != command:
            raise ValueError(f"composition intent identity conflict: {command_id}")
        if existing is None:
            uow.save_command(command)
        return command_id

    return handle


def _registry(fixtures: _CustomerFixtures) -> BoundaryConsumerRegistry:
    registry = BoundaryConsumerRegistry()
    registry.register(
        destination_domain="warehouse_fulfillment",
        contract_name="o2c.fulfillment_requested",
        contract_version=1,
        handler=_intent_handler(
            intent_name="composition.request_fulfillment_inventory",
            entity_type="warehouse_fulfillment_order",
            entity_id=fixtures.fulfillment.order_id,
        ),
    )
    registry.register(
        destination_domain="warehouse_fulfillment",
        contract_name="o2c.fulfillment_requested",
        contract_version=2,
        handler=_intent_handler(
            intent_name="composition.request_fulfillment_inventory",
            entity_type="warehouse_fulfillment_order",
            entity_id=fixtures.fulfillment.order_id,
        ),
    )
    registry.register(
        destination_domain="warehouse_management",
        contract_name="warehouse.inventory_reservation_requested",
        contract_version=1,
        handler=_intent_handler(
            intent_name="composition.reserve_fulfillment_inventory",
            entity_type="warehouse_management_stock",
            entity_id=fixtures.warehouse.origin_stock_id,
        ),
    )
    registry.register(
        destination_domain="warehouse_fulfillment",
        contract_name="warehouse.inventory_reserved",
        contract_version=1,
        handler=_intent_handler(
            intent_name="composition.accept_inventory_reservation",
            entity_type="warehouse_fulfillment_order",
            entity_id=fixtures.fulfillment.order_id,
        ),
    )
    registry.register(
        destination_domain="warehouse_management",
        contract_name="warehouse.inventory_consumption_requested",
        contract_version=1,
        handler=_intent_handler(
            intent_name="composition.consume_fulfillment_inventory",
            entity_type="warehouse_management_stock",
            entity_id=fixtures.warehouse.origin_stock_id,
        ),
    )
    registry.register(
        destination_domain="logistics",
        contract_name="warehouse.dispatch_ready",
        contract_version=1,
        handler=_intent_handler(
            intent_name="composition.deliver_shipment",
            entity_type="shipment",
            entity_id=fixtures.logistics.shipment_id,
        ),
    )
    registry.register(
        destination_domain="order_to_cash",
        contract_name="logistics.delivery_completed",
        contract_version=1,
        handler=_intent_handler(
            intent_name="composition.complete_external_fulfillment",
            entity_type="sales_order",
            entity_id=fixtures.o2c.order_id,
        ),
    )
    registry.register(
        destination_domain="cards_payments",
        contract_name="o2c.payment_requested",
        contract_version=1,
        handler=_intent_handler(
            intent_name="composition.settle_customer_payment",
            entity_type="card_payment",
            entity_id=fixtures.payments.payment_id,
        ),
    )
    registry.register(
        destination_domain="record_to_report",
        contract_name="accounting.entry_requested",
        contract_version=1,
        handler=_intent_handler(
            intent_name="composition.post_customer_journal",
            entity_type="journal_entry",
            entity_id=fixtures.r2r.journal_id,
        ),
    )
    return registry


def _next_logical_time(
    persistence: MemoryPersistence,
    previous: datetime,
) -> datetime:
    candidate = previous + timedelta(minutes=1)
    position = persistence.simulation_position()
    if position is not None and position.logical_time > candidate:
        return position.logical_time
    return candidate


def _publish(
    service: BoundaryService,
    *,
    contract_name: str,
    source_domain: str,
    source_identity: str,
    destination_domain: str,
    occurrence_key: str,
    correlation_id: str,
    causation_id: str | None,
    produced_at: datetime,
    payload: dict[str, object],
    contract_version: int = 1,
) -> BoundaryMessage:
    message = BoundaryMessage.create(
        contract_name=contract_name,
        contract_version=contract_version,
        source_domain=source_domain,
        source_identity=source_identity,
        destination_domain=destination_domain,
        occurrence_key=occurrence_key,
        correlation_id=correlation_id,
        causation_id=causation_id,
        produced_at=produced_at,
        payload=payload,
    )
    service.publish(message)
    return message


def _consume_next(
    service: BoundaryService,
    registry: BoundaryConsumerRegistry,
    *,
    owner_id: str,
    now: datetime,
    expected_message_id: str,
) -> str:
    lease = service.claim_next(
        owner_id=owner_id,
        now=now,
        lease_duration=timedelta(hours=1),
        message_id=expected_message_id,
    )
    if lease is None:
        raise RuntimeError("expected own pending composition delivery")
    if lease.message_id != expected_message_id:
        raise RuntimeError("composition claimed another causal flow")
    consumption = service.consume(
        lease=lease,
        registry=registry,
        now=now,
    )
    return consumption.consumer_effect_id


class _CausalCommandFactory:
    def __init__(
        self,
        delegate,
        *,
        correlation_id: str,
        caused_by: Command,
    ) -> None:
        self._delegate = delegate
        self._correlation_id = correlation_id
        self._caused_by = caused_by

    def create(self, name: str, **kwargs):
        kwargs["correlation_id"] = self._correlation_id
        kwargs["caused_by"] = self._caused_by
        return self._delegate.create(name, **kwargs)


@contextmanager
def _causal_command_scope(engine, *, intent: Command, correlation_id: str):
    original = engine.context.commands
    engine.context.commands = _CausalCommandFactory(
        original,
        correlation_id=correlation_id,
        caused_by=intent,
    )
    try:
        yield
    finally:
        engine.context.commands = original


def _recovery_time(
    persistence: MemoryPersistence,
    requested_at: datetime,
) -> datetime:
    position = persistence.simulation_position()
    if position is None:
        return requested_at
    if requested_at < position.logical_time:
        raise ValueError("composition intent cannot precede committed logical time")
    return position.logical_time


def _dispatch_o2c_event(
    persistence: MemoryPersistence,
    engine,
    *,
    order_id: str,
    event: str,
    correlation_id: str,
    causation_id: str,
) -> None:
    order = persistence.entity("sales_order", order_id)
    if order is None:
        raise RuntimeError(f"missing sales order: {order_id}")
    command = engine.context.commands.create(
        event,
        target=order,
        correlation_id=correlation_id,
        key=("trading-company", order.id, event, causation_id),
    )
    command = Command(
        command_id=command.command_id,
        name=command.name,
        entity_type=command.entity_type,
        entity_id=command.entity_id,
        due_at=command.due_at,
        issued_at=command.issued_at,
        tick=command.tick,
        payload=command.payload,
        causation_id=causation_id,
        correlation_id=correlation_id,
    )
    engine.dispatch(command)


def _materialize_fulfillment_from_request(
    persistence: MemoryPersistence,
    *,
    intent: Command,
    expected_sales_order_id: str,
) -> fulfillment.WarehouseEntities:
    """Derive composed WF business state from immutable O2C payload, never a fixture."""
    payload = intent.payload
    sales_order_id = payload.get("order_id")
    if not isinstance(sales_order_id, str) or sales_order_id != expected_sales_order_id:
        raise ValueError("fulfillment request source sales order mismatch")
    # Boundary payload and immutable producer provenance are sufficient even
    # when O2C state lives in a separate authoritative store.
    if intent.causation_id is not None:
        with persistence.transaction() as uow:
            source = uow.get_boundary_message(intent.causation_id)
        if (
            source is None
            or source.contract_key not in {
                "o2c.fulfillment_requested.v1", "o2c.fulfillment_requested.v2",
            }
            or source.source_domain != "order_to_cash"
            or source.destination_domain != "warehouse_fulfillment"
            or source.source_identity != sales_order_id
            or source.payload().get("order_id") != sales_order_id
            or source.message_id != payload.get("boundary_message_id")
            or source.correlation_id != intent.correlation_id
        ):
            raise ValueError("fulfillment request boundary source identity mismatch")
    request_key = f"order:{sales_order_id}"
    derived_id = deterministic_id(
        "entity", "warehouse_fulfillment_order", "warehouse-reference", request_key
    )
    if payload.get("fulfillment_order_id") != derived_id or intent.entity_id != derived_id:
        raise ValueError("fulfillment request identity mismatch")
    quantity = payload.get("requested_quantity")
    if (
        isinstance(quantity, bool)
        or not isinstance(quantity, (int, float))
        or not isfinite(float(quantity))
        or float(quantity) <= 0
    ):
        raise ValueError("fulfillment request has invalid requested_quantity")
    sku = payload.get("sku")
    if not isinstance(sku, str) or not sku.strip():
        raise ValueError("fulfillment request has invalid sku")

    existing = persistence.entity("warehouse_fulfillment_order", derived_id)
    if existing is not None:
        attributes = existing.attributes
        if (
            attributes.get("inventory_owner") != "warehouse_management"
            or attributes.get("requested_sku") != sku
            or float(attributes.get("requested_quantity", -1)) != float(quantity)
        ):
            raise ValueError("replayed fulfillment request conflicts with durable order")
        return fulfillment.WarehouseEntities(order_id=derived_id, lot_ids=())

    created = fulfillment.seed_composed_reference(
        persistence,
        now=intent.due_at,
        requested_quantity=float(quantity),
        requested_sku=sku,
        request_key=request_key,
    )
    if created.order_id != derived_id:
        raise RuntimeError("payload-derived fulfillment identity is not deterministic")
    return created


def _execute_intent(
    persistence: MemoryPersistence,
    *,
    effect_id: str,
    fixtures: _CustomerFixtures,
    correlation_id: str,
) -> None:
    intent = persistence.command(effect_id)
    if intent is None:
        return

    if intent.name == "composition.request_fulfillment_inventory":
        order_ref = _materialize_fulfillment_from_request(
            persistence, intent=intent, expected_sales_order_id=fixtures.o2c.order_id
        )
        if order_ref.order_id != fixtures.fulfillment.order_id:
            raise ValueError("composed fulfillment request differs from current workflow")
        order = persistence.entity("warehouse_fulfillment_order", order_ref.order_id)
        if order is None or order.state != "requested":
            raise RuntimeError("inventory reservation requires requested fulfillment order")

    elif intent.name == "composition.reserve_fulfillment_inventory":
        _, engine = wm.build_runtime(persistence, now=intent.due_at)
        applied = wm.reserve_external_stock(
            persistence,
            engine,
            stock_id=str(intent.payload["stock_id"]),
            quantity=float(intent.payload["quantity"]),
            sku=str(intent.payload["sku"]),
            reservation_reference=str(intent.payload["reservation_reference"]),
            caused_by=intent,
            correlation_id=correlation_id,
        )
        if not applied:
            stock = persistence.entity(
                "warehouse_management_stock",
                str(intent.payload["stock_id"]),
            )
            reservations = (
                {}
                if stock is None
                else dict(stock.attributes.get("external_reservations", {}))
            )
            if str(intent.payload["reservation_reference"]) not in reservations:
                raise RuntimeError("warehouse management inventory reservation failed")

    elif intent.name == "composition.accept_inventory_reservation":
        _, engine = fulfillment.build_runtime(persistence, now=intent.due_at)
        with _causal_command_scope(
            engine,
            intent=intent,
            correlation_id=correlation_id,
        ):
            if not fulfillment.allocate_composed_order(
                persistence,
                engine,
                entities=fixtures.fulfillment,
                stock_reference=str(intent.payload["stock_id"]),
                reservation_reference=str(intent.payload["reservation_reference"]),
                supplied_sku=str(intent.payload["sku"]),
                quantity=float(intent.payload["quantity"]),
            ):
                raise RuntimeError("composed fulfillment allocation failed")
            order = persistence.entity(
                "warehouse_fulfillment_order", fixtures.fulfillment.order_id,
            )
            if order is None:
                raise RuntimeError("composed fulfillment order disappeared")
            if order.state in {"allocated", "picking"}:
                fulfillment.pick_composed_order(
                    persistence, engine, entities=fixtures.fulfillment,
                )
            # From picking, packed or shipped, the same durable pick occurrence
            # proves the WM request. On a replay it may also reconstruct an
            # already-durable dispatch message after WM has consumed stock.
            picked_messages = reconcile_shipped_fulfillment_egress(
                persistence, correlation_id=correlation_id,
            )
            if not picked_messages or picked_messages[0].contract_key != (
                "warehouse.inventory_consumption_requested.v1"
            ):
                raise RuntimeError("durable composed picking must emit WM consumption")
            order = persistence.entity(
                "warehouse_fulfillment_order", fixtures.fulfillment.order_id,
            )
            if order is not None and order.state == "picking":
                fulfillment.pack_order(
                    persistence, engine, entities=fixtures.fulfillment,
                )
                order = persistence.entity(
                    "warehouse_fulfillment_order", fixtures.fulfillment.order_id,
                )
            if order is not None and order.state == "packed":
                fulfillment.ship_order(
                    persistence, engine, entities=fixtures.fulfillment,
                )
            elif order is None or order.state != "shipped":
                raise RuntimeError("composed fulfillment cannot resume terminal shipment")

    elif intent.name == "composition.consume_fulfillment_inventory":
        _, engine = wm.build_runtime(persistence, now=intent.due_at)
        # False means the same reservation/consumption reference was already
        # durably applied; that is a successful idempotent retry after a crash.
        wm.consume_external_reservation(
            persistence,
            engine,
            stock_id=str(intent.payload["stock_id"]),
            quantity=float(intent.payload["quantity"]),
            sku=str(intent.payload["sku"]),
            reservation_reference=str(intent.payload["reservation_reference"]),
            consumption_reference=str(intent.payload["consumption_reference"]),
            caused_by=intent,
            correlation_id=correlation_id,
        )

    elif intent.name == "composition.deliver_shipment":
        # A worker can die after *any* statechart transition. Restart from
        # durable shipment state rather than replaying the initial schedule.
        shipment_id = fixtures.logistics.shipment_id
        shipment = persistence.entity("shipment", shipment_id)
        if shipment is None:
            raise RuntimeError("logistics shipment disappeared")
        position = persistence.simulation_position()
        restore_at = position.logical_time if position is not None else intent.due_at
        recovery_time = max(restore_at, intent.due_at)
        _, engine = logistics.build_runtime(persistence, now=restore_at)
        backend = SimPyBackend(origin=restore_at)

        with _causal_command_scope(
            engine, intent=intent, correlation_id=correlation_id,
        ):
            if shipment.state == "created":
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
                pickup_due = recovery_time + timedelta(hours=1)
                pickup = engine.context.commands.create(
                    "schedule_pickup", target=shipment, due_at=pickup_due,
                    key=("trading-company", shipment.id, "schedule-pickup", intent.command_id),
                )
                engine.context.schedules.at(pickup_due, command=pickup)
            engine.rebuild_backend(backend)
            # Rebuild at the *authoritative* persisted position, never at an
            # arbitrarily later inbound message timestamp. Then advance to
            # the requested command time without violating durable lineage.
            # Materialize reattached resource leases before running forward.
            backend.run_until(backend.now)
            if recovery_time > backend.now:
                backend.run_until(recovery_time)
            engine.context.clock.now = recovery_time

            def current_state() -> str:
                current = persistence.entity("shipment", shipment_id)
                if current is None:
                    raise RuntimeError("logistics shipment disappeared during recovery")
                return current.state

            if current_state() in {"created", "pickup_scheduled", "delayed_pickup"}:
                # For a resumed schedule, respect the existing durable due time
                # rather than scheduling a second pickup command.
                pickup_times = [
                    scheduled.due_at
                    for work in persistence.scheduled_work()
                    if (scheduled := persistence.command(work.command_id)) is not None
                    and scheduled.name == "schedule_pickup"
                    and scheduled.entity_type == "shipment"
                    and scheduled.entity_id == shipment_id
                ]
                if pickup_times:
                    pickup_due = max(backend.now, min(pickup_times))
                    if pickup_due > backend.now:
                        backend.run_until(pickup_due)
                    else:
                        backend.run_until(backend.now)
                if not logistics.reconcile_pickup(
                    persistence, engine, backend, entities=fixtures.logistics,
                ):
                    raise RuntimeError("logistics pickup failed")

            if current_state() == "picked_up":
                if not logistics.reconcile_origin_hub(
                    persistence, engine, backend, entities=fixtures.logistics,
                ):
                    raise RuntimeError("logistics origin-hub handling failed")

            if current_state() in {"at_origin_hub", "in_transfer"}:
                if not logistics.reconcile_transfer(
                    persistence, engine, backend, entities=fixtures.logistics,
                ):
                    raise RuntimeError("logistics transfer failed")

            if current_state() in {"at_destination_hub", "delayed_destination_hub"}:
                if not logistics.reconcile_delivery_dispatch(
                    persistence, engine, backend, entities=fixtures.logistics,
                    ordinal=1,
                ):
                    raise RuntimeError("logistics delivery dispatch failed")

            if current_state() == "out_for_delivery":
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
                        entities=fixtures.logistics, ordinal=1,
                    )

            if current_state() != "delivered":
                raise RuntimeError(
                    "logistics delivery recovery did not reach committed delivered state"
                )

    elif intent.name == "composition.complete_external_fulfillment":
        _, engine = o2c.build_runtime(persistence, now=intent.due_at)
        order = persistence.entity("sales_order", fixtures.o2c.order_id)
        if order is None:
            raise RuntimeError("sales order disappeared")
        if order.state == "ordered":
            _dispatch_o2c_event(
                persistence,
                engine,
                order_id=order.id,
                event="start_fulfillment",
                correlation_id=correlation_id,
                causation_id=intent.command_id,
            )
            order = persistence.entity("sales_order", order.id)
        if order is not None and order.state in {"fulfilling", "partial_fulfillment"}:
            _dispatch_o2c_event(
                persistence,
                engine,
                order_id=order.id,
                event="fulfill",
                correlation_id=correlation_id,
                causation_id=intent.command_id,
            )
            order = persistence.entity("sales_order", order.id)
        if order is not None and order.state == "fulfilled":
            _dispatch_o2c_event(
                persistence,
                engine,
                order_id=order.id,
                event="ship",
                correlation_id=correlation_id,
                causation_id=intent.command_id,
            )
            order = persistence.entity("sales_order", order.id)
        if order is not None and order.state == "shipped":
            _dispatch_o2c_event(
                persistence,
                engine,
                order_id=order.id,
                event="invoice",
                correlation_id=correlation_id,
                causation_id=intent.command_id,
            )
        o2c.ensure_receivable(persistence, engine, entities=fixtures.o2c)

    elif intent.name == "composition.settle_customer_payment":
        # Domain-scoped PostgreSQL guard protects the multi-transaction card
        # authorization/capture/settlement resource lifecycle from competing
        # workers. Distinct customers share the named processor capacity.
        guard_factory = getattr(persistence, "business_resource_guard", None)
        guard = (
            guard_factory("cards_payments.authorization_settlement")
            if callable(guard_factory) else nullcontext()
        )
        with guard:
            # Each transition commits independently, so a worker may restart at
            # authorized, captured, settlement_pending or already settled. Never
            # replay an already-committed capture or rewind the logical position.
            position = persistence.simulation_position()
            restore_at = position.logical_time if position is not None else intent.due_at
            target_at = max(restore_at, intent.due_at)
            _, engine = payments.build_runtime(persistence, now=restore_at)
            backend = SimPyBackend(origin=restore_at)
            engine.rebuild_backend(backend)
            # Reattach durable resource reservations before any release operation.
            backend.run_until(backend.now)
            if target_at > backend.now:
                backend.run_until(target_at)
            engine.context.clock.now = target_at
            payment_id = fixtures.payments.payment_id
    
            def payment_state() -> str:
                current = persistence.entity("card_payment", payment_id)
                if current is None:
                    raise RuntimeError("composed card payment disappeared")
                return current.state
    
            with _causal_command_scope(
                engine,
                intent=intent,
                correlation_id=correlation_id,
            ):
                if payment_state() == "authorization_requested":
                    if not payments.reconcile_authorization(
                        persistence, engine, backend,
                        entities=fixtures.payments, outcome="authorize",
                    ):
                        raise RuntimeError("composed payment authorization failed")
                if payment_state() == "authorized":
                    settlement_at = payments.reconcile_capture_and_schedule_settlement(
                        persistence, engine, backend, entities=fixtures.payments,
                    )
                    backend.run_until(settlement_at)
    
                if payment_state() == "captured":
                    # Recovery may occur after capture committed but before its
                    # durable settlement_due schedule was inserted.
                    pending = [
                        scheduled.due_at
                        for work in persistence.scheduled_work()
                        if (scheduled := persistence.command(work.command_id)) is not None
                        and scheduled.entity_type == "card_payment"
                        and scheduled.entity_id == payment_id
                        and scheduled.name == "settlement_due"
                    ]
                    if len(pending) > 1:
                        raise RuntimeError("card payment has ambiguous durable settlement schedules")
                    if pending:
                        due_at = pending[0]
                    else:
                        current = persistence.entity("card_payment", payment_id)
                        due_at = max(
                            backend.now,
                            (current.updated_at or backend.now) + payments.SETTLEMENT_DELAY,
                        )
                        command = engine.context.commands.create(
                            "settlement_due", target=current, due_at=due_at,
                            correlation_id=payments.flow_correlation_id(),
                            key=("cards-settlement", payment_id, "due"),
                        )
                        engine.context.schedules.at(due_at, command=command)
                    backend.run_until(max(backend.now, due_at))
    
                if payment_state() == "settlement_pending":
                    payments.reconcile_settlement(
                        persistence, engine, backend,
                        entities=fixtures.payments, outcome="success",
                    )
    
                if payment_state() != "settled":
                    raise RuntimeError("composed card payment did not settle")
                # An interruption after the terminal state transition but before
                # withdraw leaves a durable reservation that must still be freed.
                engine.resources.withdraw(backend, f"authorization-processor:{payment_id}")
                engine.resources.withdraw(backend, f"settlement-processor:{payment_id}")

    elif intent.name in {
        "composition.post_customer_journal",
        "composition.post_replenishment_journal",
    }:
        # A shared posting processor has capacity one. Coordinate the whole
        # journal statechart (which spans several durable transactions) across
        # independent PostgreSQL workers, rather than letting one falsely
        # interpret temporary resource contention as permanent business failure.
        # Other adapters retain their existing single-worker semantics.
        guard_factory = getattr(persistence, "business_resource_guard", None)
        guard = (
            guard_factory("r2r.posting_processor")
            if callable(guard_factory) else nullcontext()
        )
        with guard:
            # Journal submission and posting are separate committed transitions.
            # Restore the backend at the authoritative position before advancing
            # to the intent due time; rebuilding at a later origin is invalid.
            position = persistence.simulation_position()
            restore_at = position.logical_time if position is not None else intent.due_at
            target_at = max(restore_at, intent.due_at)
            _, engine = r2r.build_runtime(persistence, now=restore_at)
            backend = SimPyBackend(origin=restore_at)
            engine.rebuild_backend(backend)
            backend.run_until(backend.now)
            if target_at > backend.now:
                backend.run_until(target_at)
            engine.context.clock.now = target_at
            with _causal_command_scope(
                engine,
                intent=intent,
                correlation_id=correlation_id,
            ):
                if not r2r.submit_and_post_journal(
                    persistence,
                    engine,
                    backend,
                    entities=fixtures.r2r,
                ):
                    raise RuntimeError("R2R journal posting failed")
                posted = persistence.entity("journal_entry", fixtures.r2r.journal_id)
                if posted is None or posted.state != "posted":
                    raise RuntimeError("R2R journal effect lacks durable posted state")
    else:
        raise ValueError(f"unsupported Trading Company customer intent: {intent.name}")

    if intent.name in CERTIFIED_INTENTS:
        # Completion requires authoritative terminal state and a durable ACK
        # receipt; proof insert and staged Command deletion share one UoW.
        # A crash after business transition but before this commit retries
        # idempotently instead of silently asserting an ACK is an effect.
        position = persistence.simulation_position()
        completed_at = max(
            intent.due_at,
            position.logical_time if position is not None else intent.due_at,
        )
        BusinessEffectService(persistence).complete(
            effect_id=effect_id, completed_at=completed_at,
        )
    else:
        with persistence.transaction() as uow:
            current = uow.get_command(effect_id)
            if current == intent:
                uow.delete_command(effect_id)


def reconcile_shipped_fulfillment_egress(
    persistence: MemoryPersistence, *, correlation_id: str | None = None,
    max_new_messages: int | None = None,
) -> tuple[BoundaryMessage, ...]:
    """Rebuild outbound WF contracts solely from durable inbound messages and state.

    Safe after worker death between shipping and the next publish. This routine
    never reads an in-memory stage sequence, and rerunning it cannot duplicate
    immutable messages. WM consumption is required before dispatch is published.
    """
    if max_new_messages is not None and max_new_messages < 0:
        raise ValueError("max_new_messages must be >= 0")
    remaining = max_new_messages
    with persistence.transaction() as uow:
        inbound = tuple(
            (delivery, message)
            for delivery in uow.boundary_deliveries()
            if (message := uow.get_boundary_message(delivery.message_id)) is not None
        )
        accepted = {
            delivery.message_id
            for delivery, message in inbound
            if delivery.status is DeliveryStatus.CONSUMED
            and uow.get_boundary_consumption(delivery.delivery_id) is not None
        }
    messages = tuple(message for _, message in inbound)
    requests = [
        message for message in messages
        if message.contract_key in {
            "o2c.fulfillment_requested.v1", "o2c.fulfillment_requested.v2",
        }
    ]
    reservations = [
        message for message in messages
        if message.contract_key == "warehouse.inventory_reserved.v1"
        and message.message_id in accepted
        and (correlation_id is None or message.correlation_id == correlation_id)
    ]
    service = BoundaryService(persistence)
    outgoing: list[BoundaryMessage] = []

    def publish_or_verify(
        *, contract_name: str, destination_domain: str, occurrence_key: str,
        source_identity: str, correlation: str, causation: str, produced_at: datetime,
        payload: dict[str, object],
    ) -> BoundaryMessage | None:
        nonlocal remaining
        message_id = deterministic_id(
            "boundary-message", "warehouse_fulfillment", source_identity,
            contract_name, 1, destination_domain, occurrence_key,
        )
        with persistence.transaction() as uow:
            prior = uow.get_boundary_message(message_id)
        if prior is not None:
            expected = BoundaryMessage.create(
                contract_name=contract_name, contract_version=1,
                source_domain="warehouse_fulfillment", source_identity=source_identity,
                destination_domain=destination_domain, occurrence_key=occurrence_key,
                correlation_id=correlation, causation_id=causation,
                produced_at=prior.produced_at, payload=payload,
            )
            if prior != expected:
                raise ValueError("durable outbound contract conflicts with source facts")
            return prior
        if remaining is not None:
            if remaining == 0:
                return None
            remaining -= 1
        return _publish(
            service, contract_name=contract_name,
            source_domain="warehouse_fulfillment", source_identity=source_identity,
            destination_domain=destination_domain, occurrence_key=occurrence_key,
            correlation_id=correlation, causation_id=causation,
            produced_at=produced_at, payload=payload,
        )

    for reservation in sorted(reservations, key=lambda m: m.message_id):
        details = reservation.payload()
        order_id = str(details["fulfillment_order_id"])
        order = persistence.entity("warehouse_fulfillment_order", order_id)
        # Consumption is a durable *pick-completion* effect, not a shipment
        # effect. WF remains in "picking" until packed but its allocation and
        # committed pick occurrence prove actual completion.
        if order is None or order.state not in {"picking", "packed", "shipped"}:
            continue
        matching = [
            request for request in requests
            if request.correlation_id == reservation.correlation_id
            and request.payload().get("fulfillment_order_id") == order_id
        ]
        if len(matching) != 1:
            raise ValueError("shipped fulfillment lacks unique durable O2C request")
        # Contract v2 binds one shipment directly to immutable ingress.
        # Frozen v1 stays opaque and requires an unambiguous single shipment.
        request = matching[0]
        if request.contract_key == "o2c.fulfillment_requested.v2":
            shipment_id = request.payload().get("shipment_id")
            if not isinstance(shipment_id, str) or not shipment_id:
                raise ValueError("v2 fulfillment request lacks shipment identity")
            if persistence.entity("shipment", shipment_id) is None:
                raise ValueError("v2 fulfillment request references missing shipment")
            if any(
                other.message_id != request.message_id
                and other.contract_key == "o2c.fulfillment_requested.v2"
                and other.payload().get("shipment_id") == shipment_id
                for other in requests
            ):
                raise ValueError("v2 shipment has multiple fulfillment owners")
        else:
            shipment_ids = [
                entity.id for entity in persistence.entities()
                if entity.entity_type == "shipment"
            ]
            if len(shipment_ids) != 1:
                raise ValueError("legacy v1 shipment linkage is ambiguous")
            shipment_id = shipment_ids[0]
        stock_id = str(details["stock_id"])
        reservation_reference = str(details["reservation_reference"])
        allocation_ids = tuple(order.attributes.get("allocation_ids", ()))
        if len(allocation_ids) != 1:
            raise ValueError("shipped composed order needs one durable WM allocation")
        allocation = persistence.entity("warehouse_allocation", str(allocation_ids[0]))
        if allocation is None or any(
            allocation.attributes.get(name) != value
            for name, value in {
                "inventory_owner": "warehouse_management",
                "reservation_reference": reservation_reference,
                "stock_reference": stock_id,
            }.items()
        ):
            # Only the order-owned allocation can cause stock consumption.
            continue
        all_allocations = [
            persistence.entity("warehouse_allocation", str(aid))
            for aid in allocation_ids
        ]
        if not all_allocations or any(
            a is None or a.state not in {"picked", "shipped"}
            for a in all_allocations
        ):
            continue
        pick_occurrence = persistence.entity(
            "warehouse_inventory_occurrence",
            fulfillment.occurrence_id("pick", allocation.id, 1),
        )
        if (
            pick_occurrence is None
            or pick_occurrence.state != "committed"
            or pick_occurrence.attributes.get("reservation_reference") != reservation_reference
        ):
            continue
        sku = str(details["sku"])
        quantity = float(details["quantity"])
        if (
            sku != order.attributes.get("requested_sku")
            or quantity != float(order.attributes.get("requested_quantity", -1))
        ):
            raise ValueError("reservation does not reconcile to shipped fulfillment")
        consumption_reference = deterministic_id(
            "trading-company-inventory-consumption", reservation_reference,
        )
        consumption = publish_or_verify(
            contract_name="warehouse.inventory_consumption_requested",
            destination_domain="warehouse_management",
            occurrence_key="inventory-consumption-requested",
            source_identity=order_id,
            correlation=reservation.correlation_id,
            causation=reservation.message_id,
            produced_at=_next_logical_time(persistence, reservation.produced_at),
            payload={
                "fulfillment_order_id": order_id,
                "stock_id": stock_id,
                "reservation_reference": reservation_reference,
                "consumption_reference": consumption_reference,
                "sku": sku,
                "quantity": quantity,
            },
        )
        if consumption is None:
            break
        outgoing.append(consumption)
        # Durable pick completion authorizes consumption, but Logistics
        # dispatch is *separately* gated on terminal shipment and WM-owned
        # consumed reservation truth.
        if order.state != "shipped":
            continue
        stock = persistence.entity("warehouse_management_stock", stock_id)
        stock_reservations = (
            {} if stock is None else dict(stock.attributes.get("external_reservations", {}))
        )
        consumed = stock_reservations.get(reservation_reference, {}).get("consumed")
        if consumed is not True:
            continue

        dispatch = publish_or_verify(
            contract_name="warehouse.dispatch_ready",
            destination_domain="logistics",
            occurrence_key="dispatch-ready",
            source_identity=order_id,
            correlation=reservation.correlation_id,
            causation=consumption.message_id,
            produced_at=_next_logical_time(persistence, consumption.produced_at),
            payload={"fulfillment_order_id": order_id, "shipment_id": shipment_id},
        )
        if dispatch is None:
            break
        outgoing.append(dispatch)
    return tuple(outgoing)


def reconcile_completed_logistics_egress(
    persistence: MemoryPersistence, *, correlation_id: str | None = None,
    max_new_messages: int | None = None,
) -> tuple[BoundaryMessage, ...]:
    """Recover Logistics delivery completion solely from committed domain truth.

    The dispatch ACK alone is not a delivered shipment. Publish downstream
    only after the accepted Logistics Command has been executed/removed and
    the persisted shipment is actually in the delivered state.
    """
    if max_new_messages is not None and max_new_messages < 0:
        raise ValueError("max_new_messages must be >= 0")

    with persistence.transaction() as uow:
        inbound = tuple(
            (delivery, uow.get_boundary_message(delivery.message_id))
            for delivery in uow.boundary_deliveries()
        )
        requests = [
            msg for _, msg in inbound
            if msg is not None and msg.contract_key in {
                "o2c.fulfillment_requested.v1", "o2c.fulfillment_requested.v2",
            }
        ]
        ready = []
        for delivery, dispatch in inbound:
            if (
                dispatch is None
                or dispatch.contract_key != "warehouse.dispatch_ready.v1"
                or (correlation_id is not None and dispatch.correlation_id != correlation_id)
                or delivery.status is not DeliveryStatus.CONSUMED
            ):
                continue
            consumed = uow.get_boundary_consumption(delivery.delivery_id)
            if consumed is None:
                raise RuntimeError("ACKed logistics dispatch is missing consumption evidence")
            if uow.get_command(consumed.consumer_effect_id) is not None:
                continue
            payload = dispatch.payload()
            shipment_id = str(payload["shipment_id"])
            shipment = uow.get_entity("shipment", shipment_id)
            if shipment is None or shipment.state != "delivered":
                continue
            order_matches = [
                msg for msg in requests
                if msg.correlation_id == dispatch.correlation_id
                and msg.payload()["fulfillment_order_id"] == payload["fulfillment_order_id"]
            ]
            if len(order_matches) != 1:
                raise RuntimeError("delivered shipment lacks a unique durable sales order cause")
            source = order_matches[0]
            if (
                source.contract_key == "o2c.fulfillment_requested.v2"
                and source.payload().get("shipment_id") != shipment_id
            ):
                raise ValueError("Logistics egress contradicts v2 immutable shipment owner")
            ready.append((dispatch, shipment_id, str(source.payload()["order_id"])))

    service = BoundaryService(persistence)
    outgoing: list[BoundaryMessage] = []
    for dispatch, shipment_id, sales_order_id in sorted(ready, key=lambda item: item[0].message_id):
        identity = deterministic_id(
            "boundary-message", "logistics", shipment_id,
            "logistics.delivery_completed", 1, "order_to_cash", "delivery-completed",
        )
        with persistence.transaction() as uow:
            prior = uow.get_boundary_message(identity)
        produced_at = (
            prior.produced_at if prior is not None
            else _next_logical_time(persistence, dispatch.produced_at)
        )
        message = BoundaryMessage.create(
            contract_name="logistics.delivery_completed", contract_version=1,
            source_domain="logistics", source_identity=shipment_id,
            destination_domain="order_to_cash", occurrence_key="delivery-completed",
            correlation_id=dispatch.correlation_id, causation_id=dispatch.message_id,
            produced_at=produced_at,
            payload={"shipment_id": shipment_id, "order_id": sales_order_id},
        )
        if prior is not None:
            if prior != message:
                raise ValueError("durable Logistics egress conflicts with source facts")
            outgoing.append(prior)
            continue
        if max_new_messages is not None and max_new_messages == 0:
            break
        if max_new_messages is not None:
            max_new_messages -= 1
        service.publish(message)
        outgoing.append(message)
    return tuple(outgoing)


def reconcile_invoiced_o2c_egress(
    persistence: MemoryPersistence, *, correlation_id: str | None = None,
    max_new_messages: int | None = None,
) -> tuple[BoundaryMessage, ...]:
    """Derive payment requests from committed O2C invoicing and causal ACKs.

    Do not mistake the Logistics receipt for applied O2C state, and do not
    infer a payment ID when the durable cardinality is ambiguous. This is the
    PC6 reference-domain contract, not a generic payment-routing heuristic.
    """
    if max_new_messages is not None and max_new_messages < 0:
        raise ValueError("max_new_messages must be >= 0")

    with persistence.transaction() as uow:
        inbound = tuple(
            (delivery, uow.get_boundary_message(delivery.message_id))
            for delivery in uow.boundary_deliveries()
        )
        ready = []
        for delivery, message in inbound:
            if (
                message is None
                or message.contract_key != "logistics.delivery_completed.v1"
                or (correlation_id is not None and message.correlation_id != correlation_id)
                or delivery.status is not DeliveryStatus.CONSUMED
            ):
                continue
            consumption = uow.get_boundary_consumption(delivery.delivery_id)
            if consumption is None:
                raise RuntimeError("ACKed O2C delivery is missing consumption evidence")
            if uow.get_command(consumption.consumer_effect_id) is not None:
                continue
            order_id = str(message.payload()["order_id"])
            order = uow.get_entity("sales_order", order_id)
            if order is None or order.state != "invoiced":
                continue
            receivable = uow.get_entity("receivable", o2c.receivable_id(order_id))
            if receivable is None or receivable.state != "open":
                continue
            amount = order.attributes["amount"]
            currency = order.attributes["currency"]
            if (
                receivable.attributes.get("order_id") != order_id
                or receivable.attributes.get("amount") != amount
                or receivable.attributes.get("currency") != currency
            ):
                raise ValueError("receivable does not certify the invoiced sales order")
            binding = uow.get_customer_settlement_binding("order", order_id)
            if binding is None:
                raise RuntimeError("O2C payment destination lacks explicit durable settlement binding")
            if (
                binding.correlation_id != message.correlation_id
                or binding.amount != amount
                or binding.currency != currency
            ):
                raise ValueError("O2C settlement binding contradicts the causally invoiced order")
            bound_payment = uow.get_entity("card_payment", binding.payment_id)
            if bound_payment is None:
                raise RuntimeError("O2C bound card payment is missing")
            if (
                bound_payment.attributes.get("amount") != amount
                or bound_payment.attributes.get("currency") != currency
            ):
                raise ValueError("O2C bound card payment conflicts with the invoiced order")
            ready.append((message, order_id, amount, currency, binding.payment_id))

    service = BoundaryService(persistence)
    emitted: list[BoundaryMessage] = []
    for upstream, order_id, amount, currency, payment_id in sorted(
        ready, key=lambda item: item[0].message_id
    ):
        identity = deterministic_id(
            "boundary-message", "order_to_cash", order_id,
            "o2c.payment_requested", 1, "cards_payments", "payment-requested",
        )
        with persistence.transaction() as uow:
            previous = uow.get_boundary_message(identity)
        produced_at = (
            previous.produced_at if previous is not None
            else _next_logical_time(persistence, upstream.produced_at)
        )
        outgoing = BoundaryMessage.create(
            contract_name="o2c.payment_requested", contract_version=1,
            source_domain="order_to_cash", source_identity=order_id,
            destination_domain="cards_payments", occurrence_key="payment-requested",
            correlation_id=upstream.correlation_id,
            causation_id=upstream.message_id, produced_at=produced_at,
            payload={
                "order_id": order_id, "payment_id": payment_id,
                "amount": amount, "currency": currency,
            },
        )
        if previous is not None:
            if previous != outgoing:
                raise ValueError("durable O2C payment request conflicts with source facts")
            emitted.append(previous)
            continue
        if max_new_messages is not None and max_new_messages == 0:
            break
        service.publish(outgoing)
        if max_new_messages is not None:
            max_new_messages -= 1
        emitted.append(outgoing)
    return tuple(emitted)


def reconcile_settled_payment_egress(
    persistence: MemoryPersistence, *, correlation_id: str | None = None,
    max_new_messages: int | None = None,
) -> tuple[BoundaryMessage, ...]:
    """Emit R2R work only after committed payment settlement business truth."""
    if max_new_messages is not None and max_new_messages < 0:
        raise ValueError("max_new_messages must be >= 0")
    with persistence.transaction() as uow:
        ready = []
        for delivery in uow.boundary_deliveries():
            upstream = uow.get_boundary_message(delivery.message_id)
            if (
                upstream is None
                or upstream.contract_key != "o2c.payment_requested.v1"
                or (correlation_id is not None and upstream.correlation_id != correlation_id)
                or delivery.status is not DeliveryStatus.CONSUMED
            ):
                continue
            receipt = uow.get_boundary_consumption(delivery.delivery_id)
            if receipt is None:
                raise RuntimeError("settlement egress requires durable payment ACK receipt")
            if uow.get_command(receipt.consumer_effect_id) is not None:
                continue
            payload = upstream.payload()
            payment_id = str(payload["payment_id"])
            payment = uow.get_entity("card_payment", payment_id)
            if payment is None or payment.state != "settled":
                continue
            if (
                payment.attributes.get("amount") != payload["amount"]
                or payment.attributes.get("currency") != payload["currency"]
            ):
                raise ValueError("settled card payment conflicts with committed request")
            binding = uow.get_customer_settlement_binding("payment", payment_id)
            if binding is None:
                raise RuntimeError("settlement journal lacks explicit durable accounting binding")
            if (
                binding.order_id != payload["order_id"]
                or binding.correlation_id != upstream.correlation_id
                or binding.amount != payload["amount"]
                or binding.currency != payload["currency"]
            ):
                raise ValueError("settled payment contradicts its immutable accounting binding")
            bound_journal = uow.get_entity("journal_entry", binding.journal_id)
            if bound_journal is None:
                raise RuntimeError("settlement bound journal is missing")
            if (
                bound_journal.attributes.get("amount") != binding.amount
                or bound_journal.attributes.get("currency") != binding.currency
            ):
                raise ValueError("settlement bound journal conflicts with original payment")
            ready.append((
                upstream, payment_id, payload["amount"], payload["currency"],
                binding.journal_id,
            ))

    service = BoundaryService(persistence)
    results: list[BoundaryMessage] = []
    for upstream, payment_id, amount, currency, journal_id in sorted(
        ready, key=lambda item: item[0].message_id
    ):
        identity = deterministic_id(
            "boundary-message", "cards_payments", payment_id,
            "accounting.entry_requested", 1, "record_to_report",
            "customer-settlement-entry",
        )
        with persistence.transaction() as uow:
            existing = uow.get_boundary_message(identity)
        produced_at = (
            existing.produced_at if existing is not None
            else _next_logical_time(persistence, upstream.produced_at)
        )
        message = BoundaryMessage.create(
            contract_name="accounting.entry_requested", contract_version=1,
            source_domain="cards_payments", source_identity=payment_id,
            destination_domain="record_to_report",
            occurrence_key="customer-settlement-entry",
            correlation_id=upstream.correlation_id,
            causation_id=upstream.message_id, produced_at=produced_at,
            payload={
                "payment_id": payment_id, "journal_id": journal_id,
                "amount": amount, "currency": currency,
            },
        )
        if existing is not None:
            if existing != message:
                raise ValueError("durable settled-payment egress conflicts with source facts")
            results.append(existing)
            continue
        if max_new_messages is not None and max_new_messages == 0:
            break
        service.publish(message)
        if max_new_messages is not None:
            max_new_messages -= 1
        results.append(message)
    return tuple(results)


def run_customer_demand_path(
    *, persistence: MemoryPersistence | None = None, instance_key: str | None = None,
) -> CustomerDemandPathResult:
    """Execute the durable Trading Company customer-demand composition path.

    Composed fulfillment delegates stock reservation/consumption to Warehouse
    Management and never creates authoritative Warehouse Fulfillment inventory lots.
    """

    if persistence is not None and instance_key is None:
        raise ValueError("instance_key is required for injected persistence")
    if instance_key is not None and (not isinstance(instance_key, str) or not instance_key.strip()):
        raise ValueError("instance_key must be a nonempty string")
    persistence = persistence if persistence is not None else MemoryPersistence()
    position = persistence.simulation_position()
    origin = max(
        o2c.ORIGIN, position.logical_time if position is not None else o2c.ORIGIN,
    )
    amount = 250.0
    currency = "USD"
    requested_quantity = 10.0
    if instance_key is not None:
        order_id = deterministic_id(
            "entity", "sales_order", "o2c-reference", "order-1", instance_key,
        )
        if persistence.entity("sales_order", order_id) is not None:
            raise ValueError(
                "customer instance already initialized: use durable recovery instead of reseeding"
            )

    sales_order = o2c.seed_reference(
        persistence, now=origin, amount=amount, currency=currency,
        instance_key=instance_key,
    )
    fulfillment_order_id = deterministic_id(
        "entity", "warehouse_fulfillment_order",
        "warehouse-reference", f"order:{sales_order.order_id}",
    )
    fixtures = _CustomerFixtures(
        o2c=sales_order,
        # A reference only: the actual order is created by the durable ingress
        # intent from the O2C boundary payload.
        fulfillment=fulfillment.WarehouseEntities(
            order_id=fulfillment_order_id, lot_ids=(),
        ),
        warehouse=wm.seed_reference(
            persistence,
            now=origin,
            origin_on_hand=20.0,
            transfer_quantity=1.0,
            sku=fulfillment.PRIMARY_SKU,
            instance_key=instance_key,
        ),
        logistics=logistics.seed_reference(
            persistence,
            now=origin,
            instance_key=instance_key,
        ),
        payments=payments.seed_reference(
            persistence,
            now=origin,
            amount=amount,
            currency=currency,
            instance_key=instance_key,
        ),
        r2r=r2r.seed_reference(
            persistence,
            now=origin,
            amount=amount,
            currency=currency,
            instance_key=instance_key,
        ),
    )

    _, o2c_engine = o2c.build_runtime(persistence, now=origin)
    if not o2c.reconcile_credit(
        persistence,
        o2c_engine,
        entities=fixtures.o2c,
    ):
        raise RuntimeError("O2C credit approval failed")

    correlation_id = deterministic_id(
        "trading-company-customer-demand",
        fixtures.o2c.order_id,
    )
    reservation_reference = deterministic_id(
        "trading-company-inventory-reservation",
        fixtures.fulfillment.order_id,
        fixtures.warehouse.origin_stock_id,
    )
    consumption_reference = deterministic_id(
        "trading-company-inventory-consumption",
        reservation_reference,
    )

    # Bind by durable business identity before publishing the first event.
    # This makes independent orders with equal money amounts unambiguous
    # even after a complete process restart.
    SettlementBindingService(persistence).bind(CustomerSettlementBinding.create(
        order_id=fixtures.o2c.order_id,
        payment_id=fixtures.payments.payment_id,
        journal_id=fixtures.r2r.journal_id,
        amount=amount,
        currency=currency,
        correlation_id=correlation_id,
    ))

    service = BoundaryService(persistence)
    registry = _registry(fixtures)
    messages: list[BoundaryMessage] = []
    effects: list[str] = []

    def consume_published_boundary(message: BoundaryMessage, *, owner_id: str) -> None:
        """Apply already-durable boundary effects with the normal leased consumer."""
        messages.append(message)
        effect = _consume_next(
            service,
            registry,
            owner_id=owner_id,
            now=message.produced_at,
            expected_message_id=message.message_id,
        )
        effects.append(effect)
        _execute_intent(
            persistence,
            effect_id=effect,
            fixtures=fixtures,
            correlation_id=correlation_id,
        )

    def publish_consume_execute(
        *,
        contract_name: str,
        source_domain: str,
        source_identity: str,
        destination_domain: str,
        occurrence_key: str,
        owner_id: str,
        payload: dict[str, object],
        contract_version: int = 1,
    ) -> BoundaryMessage:
        previous = messages[-1] if messages else None
        message = _publish(
            service,
            contract_name=contract_name,
            source_domain=source_domain,
            source_identity=source_identity,
            destination_domain=destination_domain,
            occurrence_key=occurrence_key,
            correlation_id=correlation_id,
            causation_id=None if previous is None else previous.message_id,
            produced_at=(
                origin
                if previous is None
                else _next_logical_time(persistence, previous.produced_at)
            ),
            payload=payload,
            contract_version=contract_version,
        )
        consume_published_boundary(message, owner_id=owner_id)
        return message

    publish_consume_execute(
        contract_name="o2c.fulfillment_requested",
        contract_version=2 if instance_key is not None else 1,
        source_domain="order_to_cash",
        source_identity=fixtures.o2c.order_id,
        destination_domain="warehouse_fulfillment",
        occurrence_key="fulfillment-requested",
        owner_id="warehouse-fulfillment-worker",
        payload={
            "order_id": fixtures.o2c.order_id,
            "fulfillment_order_id": fixtures.fulfillment.order_id,
            "requested_quantity": requested_quantity,
            "sku": fulfillment.PRIMARY_SKU,
            **({"shipment_id": fixtures.logistics.shipment_id}
               if instance_key is not None else {}),
        },
    )
    publish_consume_execute(
        contract_name="warehouse.inventory_reservation_requested",
        source_domain="warehouse_fulfillment",
        source_identity=fixtures.fulfillment.order_id,
        destination_domain="warehouse_management",
        occurrence_key="inventory-reservation-requested",
        owner_id="warehouse-management-worker",
        payload={
            "fulfillment_order_id": fixtures.fulfillment.order_id,
            "stock_id": fixtures.warehouse.origin_stock_id,
            "reservation_reference": reservation_reference,
            "sku": fulfillment.PRIMARY_SKU,
            "quantity": requested_quantity,
        },
    )
    publish_consume_execute(
        contract_name="warehouse.inventory_reserved",
        source_domain="warehouse_management",
        source_identity=fixtures.warehouse.origin_stock_id,
        destination_domain="warehouse_fulfillment",
        occurrence_key="inventory-reserved",
        owner_id="warehouse-fulfillment-worker",
        payload={
            "fulfillment_order_id": fixtures.fulfillment.order_id,
            "stock_id": fixtures.warehouse.origin_stock_id,
            "reservation_reference": reservation_reference,
            "sku": fulfillment.PRIMARY_SKU,
            "quantity": requested_quantity,
        },
    )
    # The shipped order and accepted WM allocation, not a caller stage list,
    # are the authoritative source for outbound consumption and dispatch.
    reconstructed = reconcile_shipped_fulfillment_egress(
        persistence, correlation_id=correlation_id,
    )
    if len(reconstructed) != 1 or reconstructed[0].contract_key != (
        "warehouse.inventory_consumption_requested.v1"
    ):
        raise RuntimeError("shipped fulfillment must durably request WM consumption")
    consume_published_boundary(
        reconstructed[0], owner_id="warehouse-management-worker",
    )
    reconstructed = reconcile_shipped_fulfillment_egress(
        persistence, correlation_id=correlation_id,
    )
    if len(reconstructed) != 2 or reconstructed[1].contract_key != (
        "warehouse.dispatch_ready.v1"
    ):
        raise RuntimeError("WM consumption must precede durable dispatch")
    consume_published_boundary(reconstructed[1], owner_id="logistics-worker")
    publish_consume_execute(
        contract_name="logistics.delivery_completed",
        source_domain="logistics",
        source_identity=fixtures.logistics.shipment_id,
        destination_domain="order_to_cash",
        occurrence_key="delivery-completed",
        owner_id="o2c-worker",
        payload={
            "shipment_id": fixtures.logistics.shipment_id,
            "order_id": fixtures.o2c.order_id,
        },
    )
    publish_consume_execute(
        contract_name="o2c.payment_requested",
        source_domain="order_to_cash",
        source_identity=fixtures.o2c.order_id,
        destination_domain="cards_payments",
        occurrence_key="payment-requested",
        owner_id="payments-worker",
        payload={
            "order_id": fixtures.o2c.order_id,
            "payment_id": fixtures.payments.payment_id,
            "amount": amount,
            "currency": currency,
        },
    )
    publish_consume_execute(
        contract_name="accounting.entry_requested",
        source_domain="cards_payments",
        source_identity=fixtures.payments.payment_id,
        destination_domain="record_to_report",
        occurrence_key="customer-settlement-entry",
        owner_id="r2r-worker",
        payload={
            "payment_id": fixtures.payments.payment_id,
            "journal_id": fixtures.r2r.journal_id,
            "amount": amount,
            "currency": currency,
        },
    )

    return CustomerDemandPathResult(
        persistence=persistence,
        correlation_id=correlation_id,
        o2c_order_id=fixtures.o2c.order_id,
        fulfillment_order_id=fixtures.fulfillment.order_id,
        shipment_id=fixtures.logistics.shipment_id,
        payment_id=fixtures.payments.payment_id,
        journal_id=fixtures.r2r.journal_id,
        warehouse_stock_id=fixtures.warehouse.origin_stock_id,
        reservation_reference=reservation_reference,
        consumption_reference=consumption_reference,
        message_ids=tuple(message.message_id for message in messages),
        effect_ids=tuple(effects),
    )


__all__ = ["CustomerDemandPathResult", "run_customer_demand_path"]

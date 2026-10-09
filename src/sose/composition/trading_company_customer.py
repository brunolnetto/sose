from __future__ import annotations

from contextlib import contextmanager
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
from .model import BoundaryMessage


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
) -> BoundaryMessage:
    message = BoundaryMessage.create(
        contract_name=contract_name,
        contract_version=1,
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
) -> str:
    lease = service.claim_next(
        owner_id=owner_id,
        now=now,
        lease_duration=timedelta(hours=1),
    )
    if lease is None:
        raise RuntimeError("expected one pending composition delivery")
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
    if persistence.entity("sales_order", sales_order_id) is None:
        raise ValueError("fulfillment request has no durable source sales order")
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
            fulfillment.pick_composed_order(
                persistence,
                engine,
                entities=fixtures.fulfillment,
            )
            fulfillment.pack_order(
                persistence,
                engine,
                entities=fixtures.fulfillment,
            )
            fulfillment.ship_order(
                persistence,
                engine,
                entities=fixtures.fulfillment,
            )

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
        for work in persistence.scheduled_work():
            scheduled = persistence.command(work.command_id)
            if (
                scheduled is not None
                and scheduled.entity_type == "shipment"
                and scheduled.entity_id == fixtures.logistics.shipment_id
                and scheduled.name == "schedule_pickup"
            ):
                with persistence.transaction() as uow:
                    uow.delete_scheduled_work(work.work_id)
                    uow.delete_command(scheduled.command_id)

        recovery_time = _recovery_time(persistence, intent.due_at)
        _, engine = logistics.build_runtime(persistence, now=recovery_time)
        backend = SimPyBackend(origin=recovery_time)
        with _causal_command_scope(
            engine,
            intent=intent,
            correlation_id=correlation_id,
        ):
            shipment = persistence.entity("shipment", fixtures.logistics.shipment_id)
            if shipment is None:
                raise RuntimeError("logistics shipment disappeared")
            pickup_due = max(recovery_time, intent.due_at) + timedelta(hours=1)
            pickup = engine.context.commands.create(
                "schedule_pickup",
                target=shipment,
                due_at=pickup_due,
                key=(
                    "trading-company",
                    shipment.id,
                    "schedule-pickup",
                    intent.command_id,
                ),
            )
            engine.context.schedules.at(pickup_due, command=pickup)
            engine.rebuild_backend(backend)
            backend.run_until(pickup_due)
            if not logistics.reconcile_pickup(
                persistence,
                engine,
                backend,
                entities=fixtures.logistics,
            ):
                raise RuntimeError("logistics pickup failed")
            if not logistics.reconcile_origin_hub(
                persistence,
                engine,
                backend,
                entities=fixtures.logistics,
            ):
                raise RuntimeError("logistics origin-hub handling failed")
            if not logistics.reconcile_transfer(
                persistence,
                engine,
                backend,
                entities=fixtures.logistics,
            ):
                raise RuntimeError("logistics transfer failed")
            if not logistics.reconcile_delivery_dispatch(
                persistence,
                engine,
                backend,
                entities=fixtures.logistics,
                ordinal=1,
            ):
                raise RuntimeError("logistics delivery dispatch failed")
            logistics.reconcile_delivery_success(
                persistence,
                engine,
                backend,
                entities=fixtures.logistics,
                ordinal=1,
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
        recovery_time = _recovery_time(persistence, intent.due_at)
        _, engine = payments.build_runtime(persistence, now=recovery_time)
        backend = SimPyBackend(origin=recovery_time)
        engine.rebuild_backend(backend)
        if backend.now < intent.due_at:
            backend.run_until(intent.due_at)
        with _causal_command_scope(
            engine,
            intent=intent,
            correlation_id=correlation_id,
        ):
            if not payments.reconcile_authorization(
                persistence,
                engine,
                backend,
                entities=fixtures.payments,
                outcome="authorize",
            ):
                raise RuntimeError("payment authorization failed")
            settlement_at = payments.reconcile_capture_and_schedule_settlement(
                persistence,
                engine,
                backend,
                entities=fixtures.payments,
            )
            backend.run_until(settlement_at)
            payments.reconcile_settlement(
                persistence,
                engine,
                backend,
                entities=fixtures.payments,
                outcome="success",
            )

    elif intent.name == "composition.post_customer_journal":
        recovery_time = _recovery_time(persistence, intent.due_at)
        _, engine = r2r.build_runtime(persistence, now=recovery_time)
        backend = SimPyBackend(origin=recovery_time)
        engine.rebuild_backend(backend)
        if backend.now < intent.due_at:
            backend.run_until(intent.due_at)
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

    else:
        raise ValueError(f"unsupported Trading Company customer intent: {intent.name}")

    with persistence.transaction() as uow:
        current = uow.get_command(effect_id)
        if current == intent:
            uow.delete_command(effect_id)


def reconcile_shipped_fulfillment_egress(
    persistence: MemoryPersistence, *, correlation_id: str | None = None,
) -> tuple[BoundaryMessage, ...]:
    """Rebuild outbound WF contracts solely from durable inbound messages and state.

    Safe after worker death between shipping and the next publish. This routine
    never reads an in-memory stage sequence, and rerunning it cannot duplicate
    immutable messages. WM consumption is required before dispatch is published.
    """
    with persistence.transaction() as uow:
        inbound = tuple(
            message
            for delivery in uow.boundary_deliveries()
            if (message := uow.get_boundary_message(delivery.message_id)) is not None
        )
    requests = [
        message for message in inbound
        if message.contract_key == "o2c.fulfillment_requested.v1"
    ]
    reservations = [
        message for message in inbound
        if message.contract_key == "warehouse.inventory_reserved.v1"
        and (correlation_id is None or message.correlation_id == correlation_id)
    ]
    service = BoundaryService(persistence)
    outgoing: list[BoundaryMessage] = []

    def publish_or_verify(
        *, contract_name: str, destination_domain: str, occurrence_key: str,
        source_identity: str, correlation: str, causation: str, produced_at: datetime,
        payload: dict[str, object],
    ) -> BoundaryMessage:
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
        if order is None or order.state != "shipped":
            continue
        matching = [
            request for request in requests
            if request.correlation_id == reservation.correlation_id
            and request.payload().get("fulfillment_order_id") == order_id
        ]
        if len(matching) != 1:
            raise ValueError("shipped fulfillment lacks unique durable O2C request")
        request_payload = matching[0].payload()
        shipment_id = request_payload.get("shipment_id")
        if not isinstance(shipment_id, str) or not shipment_id:
            raise ValueError("shipment reference missing from durable O2C request")
        if persistence.entity("shipment", shipment_id) is None:
            raise ValueError("referenced logistics shipment is not durable")
        stock_id = str(details["stock_id"])
        reservation_reference = str(details["reservation_reference"])
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
        outgoing.append(consumption)
        stock = persistence.entity("warehouse_management_stock", stock_id)
        stock_reservations = (
            {} if stock is None else dict(stock.attributes.get("external_reservations", {}))
        )
        consumed = stock_reservations.get(reservation_reference, {}).get("consumed")
        if consumed is not True:
            continue

        outgoing.append(publish_or_verify(
            contract_name="warehouse.dispatch_ready",
            destination_domain="logistics",
            occurrence_key="dispatch-ready",
            source_identity=order_id,
            correlation=reservation.correlation_id,
            causation=consumption.message_id,
            produced_at=_next_logical_time(persistence, consumption.produced_at),
            payload={"fulfillment_order_id": order_id, "shipment_id": shipment_id},
        ))
    return tuple(outgoing)


def run_customer_demand_path() -> CustomerDemandPathResult:
    """Execute the durable Trading Company customer-demand composition path.

    Composed fulfillment delegates stock reservation/consumption to Warehouse
    Management and never creates authoritative Warehouse Fulfillment inventory lots.
    """

    persistence = MemoryPersistence()
    origin = o2c.ORIGIN
    amount = 250.0
    currency = "USD"
    requested_quantity = 10.0

    sales_order = o2c.seed_reference(
        persistence, now=origin, amount=amount, currency=currency,
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
        ),
        logistics=logistics.seed_reference(
            persistence,
            now=origin,
        ),
        payments=payments.seed_reference(
            persistence,
            now=origin,
            amount=amount,
            currency=currency,
        ),
        r2r=r2r.seed_reference(
            persistence,
            now=origin,
            amount=amount,
            currency=currency,
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

    service = BoundaryService(persistence)
    registry = _registry(fixtures)
    messages: list[BoundaryMessage] = []
    effects: list[str] = []

    def publish_consume_execute(
        *,
        contract_name: str,
        source_domain: str,
        source_identity: str,
        destination_domain: str,
        occurrence_key: str,
        owner_id: str,
        payload: dict[str, object],
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
        )
        messages.append(message)
        effect = _consume_next(
            service,
            registry,
            owner_id=owner_id,
            now=message.produced_at,
        )
        effects.append(effect)
        _execute_intent(
            persistence,
            effect_id=effect,
            fixtures=fixtures,
            correlation_id=correlation_id,
        )
        return message

    publish_consume_execute(
        contract_name="o2c.fulfillment_requested",
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
            "shipment_id": fixtures.logistics.shipment_id,
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
    publish_consume_execute(
        contract_name="warehouse.inventory_consumption_requested",
        source_domain="warehouse_fulfillment",
        source_identity=fixtures.fulfillment.order_id,
        destination_domain="warehouse_management",
        occurrence_key="inventory-consumption-requested",
        owner_id="warehouse-management-worker",
        payload={
            "fulfillment_order_id": fixtures.fulfillment.order_id,
            "stock_id": fixtures.warehouse.origin_stock_id,
            "reservation_reference": reservation_reference,
            "consumption_reference": consumption_reference,
            "sku": fulfillment.PRIMARY_SKU,
            "quantity": requested_quantity,
        },
    )
    publish_consume_execute(
        contract_name="warehouse.dispatch_ready",
        source_domain="warehouse_fulfillment",
        source_identity=fixtures.fulfillment.order_id,
        destination_domain="logistics",
        occurrence_key="dispatch-ready",
        owner_id="logistics-worker",
        payload={
            "fulfillment_order_id": fixtures.fulfillment.order_id,
            "shipment_id": fixtures.logistics.shipment_id,
        },
    )
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

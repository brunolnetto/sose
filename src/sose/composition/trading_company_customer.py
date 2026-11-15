from __future__ import annotations

from contextlib import contextmanager, nullcontext  # noqa: F401
from dataclasses import dataclass
from datetime import datetime, timedelta  # noqa: F401
from collections.abc import Mapping
from math import isfinite  # noqa: F401
from time import monotonic, sleep

from sose.backends.simpy import SimPyBackend  # noqa: F401
from sose.core.events import Command
from sose.core.identity import deterministic_id
from sose.core.resource_reservations import ResourceConflictError, TemporalReservation, authoritative_effect_reservation_id
from sose.persistence.memory import MemoryPersistence

from sose.examples.order_to_cash import simulation as o2c
from sose.examples.warehouse_fulfillment import simulation as fulfillment  # noqa: F401
from sose.examples.logistics import simulation as logistics  # noqa: F401
from sose.examples.cards_payments import simulation as payments  # noqa: F401
from sose.examples.record_to_report import simulation as r2r  # noqa: F401
from sose.examples.warehouse_management import simulation as wm  # noqa: F401

from .boundary import BoundaryConsumerRegistry, BoundaryService
from .effects import BusinessEffectService, CERTIFIED_INTENTS  # noqa: F401
from .bindings import CustomerSettlementBinding, SettlementBindingService
from .model import BoundaryMessage, DeliveryStatus  # noqa: F401
from .trading_company_customer_stages import (
    _delivery_completed_stage,
    _demand_origin,
    _fulfillment_requested_stage,
    _inventory_reserved_stage,
    _journal_requested_stage,
    _payment_requested_stage,
    _require_demand_path_arguments,
    _require_uninitialized_instance,
    _reservation_requested_stage,
    _seed_customer_fixtures,
)
from .trading_company_customer_support import (  # noqa: F401 - historical import location
    DurableResourceWait,
    _CausalCommandFactory,
    _CustomerFixtures,
    _IntentExecution,
    _causal_command_scope,
    _complete_intent,
    _consume_next,
    _dispatch_o2c_event,
    _intent_handler,
    _next_logical_time,
    _operational_owner,
    _publish,
    _raise_delivery_dispatch_failure_or_wait,
    _recovery_time,
)
from .trading_company_delivery_intent import (  # noqa: F401 - historical import location
    _execute_deliver_shipment,
    pickup_resource_due_at,
)
from .trading_company_finance_intents import (
    _execute_complete_external_fulfillment,
    _execute_post_journal,
    _execute_settle_customer_payment,
)
from .trading_company_fulfillment_egress import _reconcile_shipped_fulfillment_egress
from .trading_company_fulfillment_intents import (  # noqa: F401 - historical import location
    _execute_accept_inventory_reservation,
    _execute_consume_fulfillment_inventory,
    _execute_request_fulfillment_inventory,
    _execute_reserve_fulfillment_inventory,
    _materialize_fulfillment_from_request,
)
from .trading_company_logistics_egress import (  # noqa: F401 - historical import location
    reconcile_completed_logistics_egress,
)
from .trading_company_settlement_egress import (  # noqa: F401 - historical import location
    reconcile_invoiced_o2c_egress,
    reconcile_settled_payment_egress,
)


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


def _registry(fixtures: _CustomerFixtures, *, owner_id: str) -> BoundaryConsumerRegistry:
    registry = BoundaryConsumerRegistry()
    registry.register(
        destination_domain="warehouse_fulfillment",
        contract_name="o2c.fulfillment_requested",
        source_domain="order_to_cash",
        contract_version=1,
        handler=_intent_handler(
            intent_name="composition.request_fulfillment_inventory",
            entity_type="warehouse_fulfillment_order",
            entity_id=fixtures.fulfillment.order_id,
            owner_id=owner_id,
        ),
    )
    registry.register(
        destination_domain="warehouse_fulfillment",
        contract_name="o2c.fulfillment_requested",
        source_domain="order_to_cash",
        contract_version=2,
        handler=_intent_handler(
            intent_name="composition.request_fulfillment_inventory",
            entity_type="warehouse_fulfillment_order",
            entity_id=fixtures.fulfillment.order_id,
            owner_id=owner_id,
        ),
    )
    registry.register(
        destination_domain="warehouse_management",
        contract_name="warehouse.inventory_reservation_requested",
        source_domain="warehouse_fulfillment",
        contract_version=1,
        handler=_intent_handler(
            intent_name="composition.reserve_fulfillment_inventory",
            entity_type="warehouse_management_stock",
            entity_id=fixtures.warehouse.origin_stock_id,
            owner_id=owner_id,
        ),
    )
    registry.register(
        destination_domain="warehouse_fulfillment",
        contract_name="warehouse.inventory_reserved",
        source_domain="warehouse_management",
        contract_version=1,
        handler=_intent_handler(
            intent_name="composition.accept_inventory_reservation",
            entity_type="warehouse_fulfillment_order",
            entity_id=fixtures.fulfillment.order_id,
            owner_id=owner_id,
        ),
    )
    registry.register(
        destination_domain="warehouse_management",
        contract_name="warehouse.inventory_consumption_requested",
        source_domain="warehouse_fulfillment",
        contract_version=1,
        handler=_intent_handler(
            intent_name="composition.consume_fulfillment_inventory",
            entity_type="warehouse_management_stock",
            entity_id=fixtures.warehouse.origin_stock_id,
            owner_id=owner_id,
        ),
    )
    registry.register(
        destination_domain="logistics",
        contract_name="warehouse.dispatch_ready",
        source_domain="warehouse_fulfillment",
        contract_version=1,
        handler=_intent_handler(
            intent_name="composition.deliver_shipment",
            entity_type="shipment",
            entity_id=fixtures.logistics.shipment_id,
            owner_id=owner_id,
        ),
    )
    registry.register(
        destination_domain="order_to_cash",
        contract_name="logistics.delivery_completed",
        source_domain="logistics",
        contract_version=1,
        handler=_intent_handler(
            intent_name="composition.complete_external_fulfillment",
            entity_type="sales_order",
            entity_id=fixtures.o2c.order_id,
            owner_id=owner_id,
        ),
    )
    registry.register(
        destination_domain="cards_payments",
        contract_name="o2c.payment_requested",
        source_domain="order_to_cash",
        contract_version=1,
        handler=_intent_handler(
            intent_name="composition.settle_customer_payment",
            entity_type="card_payment",
            entity_id=fixtures.payments.payment_id,
            owner_id=owner_id,
        ),
    )
    registry.register(
        destination_domain="record_to_report",
        contract_name="accounting.entry_requested",
        source_domain="cards_payments",
        contract_version=1,
        handler=_intent_handler(
            intent_name="composition.post_customer_journal",
            entity_type="journal_entry",
            entity_id=fixtures.r2r.journal_id,
            owner_id=owner_id,
        ),
    )
    return registry


def _accept_inventory_reservation(run: _IntentExecution) -> None:
    # Resolved through this module so the egress keeps publishing with its `_publish`.
    _execute_accept_inventory_reservation(
        run, reconcile_shipped_egress=reconcile_shipped_fulfillment_egress,
    )


_INTENT_EXECUTORS = (
    ("composition.request_fulfillment_inventory", _execute_request_fulfillment_inventory),
    ("composition.reserve_fulfillment_inventory", _execute_reserve_fulfillment_inventory),
    ("composition.accept_inventory_reservation", _accept_inventory_reservation),
    ("composition.consume_fulfillment_inventory", _execute_consume_fulfillment_inventory),
    ("composition.deliver_shipment", _execute_deliver_shipment),
    ("composition.complete_external_fulfillment", _execute_complete_external_fulfillment),
    ("composition.settle_customer_payment", _execute_settle_customer_payment),
)


def _intent_executor(intent: Command):
    for name, executor in _INTENT_EXECUTORS:
        if intent.name == name:
            return executor
    if intent.name in {
        "composition.post_customer_journal",
        "composition.post_replenishment_journal",
    }:
        return _execute_post_journal
    raise ValueError(f"unsupported Trading Company customer intent: {intent.name}")


def _execute_intent(
    persistence: MemoryPersistence,
    *,
    effect_id: str,
    fixtures: _CustomerFixtures,
    correlation_id: str,
    authoritative_pickup: TemporalReservation | None = None,
    resource_persistence: MemoryPersistence | None = None,
    organization_id: str,
) -> None:
    if not isinstance(organization_id, str) or not organization_id.strip():
        raise ValueError("organization_id must not be empty")
    intent = persistence.command(effect_id)
    if intent is None:
        return
    expected_resource_id = authoritative_effect_reservation_id(
        effect_id,
        scope=persistence.namespace if resource_persistence is not None else None,
    )
    if authoritative_pickup is not None and (
        intent.name != "composition.deliver_shipment"
        or authoritative_pickup.reservation_id != expected_resource_id
        or authoritative_pickup.causation_id != intent.causation_id
    ):
        raise ResourceConflictError("authoritative pickup grant contradicts source effect")

    _intent_executor(intent)(_IntentExecution(
        persistence=persistence,
        intent=intent,
        effect_id=effect_id,
        fixtures=fixtures,
        correlation_id=correlation_id,
        organization_id=organization_id,
        authoritative_pickup=authoritative_pickup,
        resource_persistence=resource_persistence,
    ))
    _complete_intent(
        persistence, intent, effect_id=effect_id, organization_id=organization_id,
    )


def reconcile_shipped_fulfillment_egress(
    persistence: MemoryPersistence, *, organization_owners: Mapping[str, str],
    correlation_id: str | None = None,
    max_new_messages: int | None = None,
    recovery_job_id: str | None = None,
) -> tuple[BoundaryMessage, ...]:
    """Rebuild outbound WF contracts solely from durable inbound messages and state.

    Safe after worker death between shipping and the next publish. This routine
    never reads an in-memory stage sequence, and rerunning it cannot duplicate
    immutable messages. WM consumption is required before dispatch is published.
    """
    return _reconcile_shipped_fulfillment_egress(
        persistence, organization_owners=organization_owners,
        correlation_id=correlation_id,
        max_new_messages=max_new_messages,
        recovery_job_id=recovery_job_id,
        # Resolved at each publish so a `_publish` replaced on this module is honoured.
        publish=lambda *args, **kwargs: _publish(*args, **kwargs),
    )


class _DemandPathRun:
    """Boundary progress of one synchronous `run_customer_demand_path` call."""

    def __init__(
        self,
        persistence: MemoryPersistence,
        *,
        fixtures: _CustomerFixtures,
        correlation_id: str,
        organization_id: str,
        origin: datetime,
    ) -> None:
        self.persistence = persistence
        self.fixtures = fixtures
        self.correlation_id = correlation_id
        self.organization_id = organization_id
        self.origin = origin
        self.service = BoundaryService(persistence)
        self.registry = _registry(fixtures, owner_id=organization_id)
        self.messages: list[BoundaryMessage] = []
        self.effects: list[str] = []

    def consume_published_boundary(self, message: BoundaryMessage, *, owner_id: str) -> None:
        """Apply already-durable boundary effects with the normal leased consumer."""
        self.messages.append(message)
        effect = _consume_next(
            self.service,
            self.registry,
            owner_id=owner_id,
            now=message.produced_at,
            expected_message_id=message.message_id,
        )
        self.effects.append(effect)
        # Synchronous reference demonstration only. Production recurring
        # recovery uses a durable next tick, never wall-time busy-polling.
        deadline = monotonic() + 12.0
        while True:
            try:
                _execute_intent(
                    self.persistence,
                    effect_id=effect,
                    fixtures=self.fixtures,
                    correlation_id=self.correlation_id,
                    organization_id=self.organization_id,
                )
                break
            except DurableResourceWait:
                if monotonic() >= deadline:
                    raise
                sleep(0.02)

    def publish_consume_execute(
        self,
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
        previous = self.messages[-1] if self.messages else None
        message = _publish(
            self.service,
            contract_name=contract_name,
            source_domain=source_domain,
            source_identity=source_identity,
            destination_domain=destination_domain,
            occurrence_key=occurrence_key,
            correlation_id=self.correlation_id,
            causation_id=None if previous is None else previous.message_id,
            produced_at=(
                self.origin
                if previous is None
                else _next_logical_time(
                    self.persistence, previous.produced_at,
                    organization_id=self.organization_id,
                )
            ),
            payload=payload,
            contract_version=contract_version,
        )
        self.consume_published_boundary(message, owner_id=owner_id)
        return message


def run_customer_demand_path(
    *, organization_id: str,
    persistence: MemoryPersistence | None = None, instance_key: str | None = None,
) -> CustomerDemandPathResult:
    """Execute the durable Trading Company customer-demand composition path.

    Composed fulfillment delegates stock reservation/consumption to Warehouse
    Management and never creates authoritative Warehouse Fulfillment inventory lots.
    """

    _require_demand_path_arguments(organization_id, persistence, instance_key)
    persistence = persistence if persistence is not None else MemoryPersistence()
    origin = _demand_origin(persistence, organization_id)
    amount = 250.0
    currency = "USD"
    requested_quantity = 10.0
    _require_uninitialized_instance(persistence, instance_key)

    fixtures = _seed_customer_fixtures(
        persistence, organization_id=organization_id, origin=origin,
        amount=amount, currency=currency, instance_key=instance_key,
    )

    _, o2c_engine = o2c.build_runtime(persistence, owner_id=organization_id, now=origin)
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

    run = _DemandPathRun(
        persistence, fixtures=fixtures, correlation_id=correlation_id,
        organization_id=organization_id, origin=origin,
    )
    run.publish_consume_execute(**_fulfillment_requested_stage(
        fixtures, requested_quantity=requested_quantity, instance_key=instance_key,
    ))
    run.publish_consume_execute(**_reservation_requested_stage(
        fixtures, reservation_reference=reservation_reference,
        requested_quantity=requested_quantity,
    ))
    run.publish_consume_execute(**_inventory_reserved_stage(
        fixtures, reservation_reference=reservation_reference,
        requested_quantity=requested_quantity,
    ))
    # The shipped order and accepted WM allocation, not a caller stage list,
    # are the authoritative source for outbound consumption and dispatch.
    reconstructed = reconcile_shipped_fulfillment_egress(
        persistence, correlation_id=correlation_id,
        organization_owners={correlation_id: organization_id},
    )
    if len(reconstructed) != 1 or reconstructed[0].contract_key != (
        "warehouse.inventory_consumption_requested.v1"
    ):
        raise RuntimeError("shipped fulfillment must durably request WM consumption")
    run.consume_published_boundary(
        reconstructed[0], owner_id="warehouse-management-worker",
    )
    reconstructed = reconcile_shipped_fulfillment_egress(
        persistence, correlation_id=correlation_id,
        organization_owners={correlation_id: organization_id},
    )
    if len(reconstructed) != 2 or reconstructed[1].contract_key != (
        "warehouse.dispatch_ready.v1"
    ):
        raise RuntimeError("WM consumption must precede durable dispatch")
    run.consume_published_boundary(reconstructed[1], owner_id="logistics-worker")
    run.publish_consume_execute(**_delivery_completed_stage(fixtures))
    run.publish_consume_execute(**_payment_requested_stage(
        fixtures, amount=amount, currency=currency,
    ))
    run.publish_consume_execute(**_journal_requested_stage(
        fixtures, amount=amount, currency=currency,
    ))

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
        message_ids=tuple(message.message_id for message in run.messages),
        effect_ids=tuple(run.effects),
    )


__all__ = ["CustomerDemandPathResult", "run_customer_demand_path"]

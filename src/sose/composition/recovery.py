"""Bounded, restartable PC6 customer-boundary recovery trigger.

This runner owns no business entities. It replays accepted domain intents,
reconstructs outbound WF messages from durable facts, and applies WM consumption
through the existing boundary registry. It now resumes Logistics effects from accepted dispatches. O2C/Payments/R2R
still require separate domain worker ownership.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from typing import Mapping

from sose.core.resource_identity import ResourcePoolContract
from sose.core.resource_reservations import TemporalReservation
from sose.composition.resource_intents import IntentResourceCoordinator

from sose.composition import trading_company_customer as customer
from sose.composition.boundary import BoundaryConsumerRegistry, BoundaryService
from sose.composition.model import BoundaryMessage, DeliveryStatus
from sose.examples.warehouse_fulfillment import simulation as fulfillment
from sose.examples.logistics import simulation as logistics
from sose.examples.order_to_cash import simulation as o2c
from sose.examples.cards_payments import simulation as payments
from sose.examples.record_to_report import simulation as r2r
from sose.jobs.model import CompletedJobTrigger, SimulationJobState
from sose.persistence.base import Persistence
from sose.persistence.ownership import FencedEnginePersistence


_WM_CONTRACT = ("warehouse.inventory_consumption_requested", 1)
_LOGISTICS_CONTRACT = ("warehouse.dispatch_ready", 1)
_O2C_CONTRACT = ("logistics.delivery_completed", 1)
_PAYMENTS_CONTRACT = ("o2c.payment_requested", 1)
_R2R_CONTRACT = ("accounting.entry_requested", 1)
_RECOVERABLE = frozenset({
    "composition.accept_inventory_reservation",
    "composition.consume_fulfillment_inventory",
    "composition.deliver_shipment",
    "composition.complete_external_fulfillment",
    "composition.settle_customer_payment",
    "composition.post_customer_journal",
    "composition.post_replenishment_journal",
})


@dataclass(frozen=True, slots=True)
class RecoveryTriggerResult:
    job_id: str
    trigger_id: str
    actions: int


@dataclass(slots=True)
class TradingCustomerRecoveryRunner:
    """One trigger is a durable, bounded pass over existing authoritative state.

    Requires ownership-capable EnginePersistence. The externally scheduled
    caller supplies a durable trigger identity, and the writer epoch fences
    stale attempts. The SOSE job-state checkpoint permits restart with the
    same trigger ID after a worker death.
    """

    persistence: Persistence
    owner_id: str
    job_id: str = "trading-company-customer-recovery"
    max_actions: int = 16
    lease_duration: timedelta = timedelta(hours=1)
    resource_policies: Mapping[str, ResourcePoolContract] | None = None
    resource_organizations: Mapping[str, str] | None = None
    resource_slot_duration: timedelta = timedelta(minutes=5)
    resource_retry_delay: timedelta = timedelta(minutes=5)
    # Opt-in: one recurring job owns one correlation. No global writer lease.
    correlation_id: str | None = None
    scoped_writer: bool = False

    def __post_init__(self) -> None:
        if not self.owner_id or not self.job_id:
            raise ValueError("worker owner and job identity must be nonempty")
        if self.max_actions < 1:
            raise ValueError("max_actions must be >= 1")
        if self.lease_duration <= timedelta(0):
            raise ValueError("lease_duration must be positive")
        if self.correlation_id is not None and not self.correlation_id:
            raise ValueError("correlation_id cannot be empty")
        if self.scoped_writer:
            if self.correlation_id is None:
                raise ValueError("scoped recovery requires one explicit correlation_id")
            if not callable(getattr(self.persistence, "claim_scoped_writer", None)):
                raise TypeError("scoped recovery requires PostgreSQL scoped fencing")
        if self.resource_policies is not None:
            if self.resource_slot_duration <= timedelta(0) or self.resource_retry_delay <= timedelta(0):
                raise ValueError("resource slot and retry delay must be positive")
            if not self.resource_organizations or any(
                not correlation or not organization
                for correlation, organization in self.resource_organizations.items()
            ):
                raise ValueError(
                    "resource policies require explicit correlation-to-organization bindings"
                )
        if self.resource_policies is not None and self.correlation_id is not None:
            if self.correlation_id not in self.resource_organizations:
                raise ValueError("resource mapping lacks the owned correlation_id")
        if not self.scoped_writer and (
            not callable(getattr(self.persistence, "writer_epoch", None))
            or not callable(getattr(self.persistence, "claim_writer", None))
        ):
            raise TypeError("composition runner requires authoritative writer fencing")

    @staticmethod
    def _pending_effects(store: Persistence) -> tuple[tuple[BoundaryMessage, str], ...]:
        with store.transaction() as uow:
            pending = []
            for delivery in uow.boundary_deliveries():
                if delivery.status is not DeliveryStatus.CONSUMED:
                    continue
                consumption = uow.get_boundary_consumption(delivery.delivery_id)
                if consumption is None:
                    raise RuntimeError("consumed boundary delivery lacks durable consumption")
                intent = uow.get_command(consumption.consumer_effect_id)
                if intent is None or intent.name not in _RECOVERABLE:
                    continue
                message = uow.get_boundary_message(delivery.message_id)
                if message is None:
                    raise RuntimeError("accepted boundary effect lacks source message")
                pending.append((message, intent.command_id))
        return tuple(sorted(pending, key=lambda pair: (pair[0].produced_at, pair[0].message_id)))

    @staticmethod
    def _execute_pending(
        store: Persistence, source: BoundaryMessage, effect_id: str,
        *, authoritative_pickup: TemporalReservation | None = None,
    ) -> None:
        details = source.payload()
        order_id = str(details.get("fulfillment_order_id", ""))
        sales_order_id = str(details.get("order_id", ""))
        payment_id = str(details.get("payment_id", ""))
        journal_id = str(details.get("journal_id", ""))
        with store.transaction() as uow:
            journal = uow.get_entity("journal_entry", journal_id) if journal_id else None
        period_id = str(journal.attributes["period_id"]) if journal is not None else ""
        shipment_id = str(details.get("shipment_id", ""))
        fixture = customer._CustomerFixtures(
            o2c=o2c.O2CEntities(order_id=sales_order_id),
            fulfillment=fulfillment.WarehouseEntities(order_id=order_id, lot_ids=()),
            warehouse=None,
            logistics=logistics.LogisticsEntities(shipment_id=shipment_id),
            payments=payments.PaymentEntities(payment_id=payment_id),
            r2r=r2r.R2REntities(
                period_id=period_id, journal_id=journal_id,
                reconciliation_id="", close_task_id="",
            ),
        )
        kwargs = (
            {"authoritative_pickup": authoritative_pickup}
            if authoritative_pickup is not None else {}
        )
        customer._execute_intent(
            store,
            effect_id=effect_id,
            fixtures=fixture,
            correlation_id=source.correlation_id,
            **kwargs,
        )

    @staticmethod
    def _registry_for(message: BoundaryMessage) -> BoundaryConsumerRegistry:
        details = message.payload()
        registry = BoundaryConsumerRegistry()
        if message.contract_key == "warehouse.inventory_consumption_requested.v1":
            registry.register(
                destination_domain="warehouse_management",
                contract_name=_WM_CONTRACT[0],
                contract_version=_WM_CONTRACT[1],
                handler=customer._intent_handler(
                    intent_name="composition.consume_fulfillment_inventory",
                    entity_type="warehouse_management_stock",
                    entity_id=str(details["stock_id"]),
                ),
            )
        elif message.contract_key == "warehouse.dispatch_ready.v1":
            registry.register(
                destination_domain="logistics",
                contract_name=_LOGISTICS_CONTRACT[0],
                contract_version=_LOGISTICS_CONTRACT[1],
                handler=customer._intent_handler(
                    intent_name="composition.deliver_shipment",
                    entity_type="shipment",
                    entity_id=str(details["shipment_id"]),
                ),
            )
        elif message.contract_key == "logistics.delivery_completed.v1":
            registry.register(
                destination_domain="order_to_cash",
                contract_name=_O2C_CONTRACT[0],
                contract_version=_O2C_CONTRACT[1],
                handler=customer._intent_handler(
                    intent_name="composition.complete_external_fulfillment",
                    entity_type="sales_order",
                    entity_id=str(details["order_id"]),
                ),
            )
        elif message.contract_key == "o2c.payment_requested.v1":
            registry.register(
                destination_domain="cards_payments",
                contract_name=_PAYMENTS_CONTRACT[0],
                contract_version=_PAYMENTS_CONTRACT[1],
                handler=customer._intent_handler(
                    intent_name="composition.settle_customer_payment",
                    entity_type="card_payment",
                    entity_id=str(details["payment_id"]),
                ),
            )
        elif message.contract_key == "accounting.entry_requested.v1":
            if message.source_domain == "cards_payments":
                intent_name = "composition.post_customer_journal"
            elif message.source_domain == "procure_to_pay":
                intent_name = "composition.post_replenishment_journal"
            else:
                raise ValueError("unsupported accounting source domain for PC6 recovery")
            registry.register(
                destination_domain="record_to_report",
                contract_name=_R2R_CONTRACT[0],
                contract_version=_R2R_CONTRACT[1],
                handler=customer._intent_handler(
                    intent_name=intent_name,
                    entity_type="journal_entry",
                    entity_id=str(details["journal_id"]),
                ),
            )
        else:
            raise ValueError("recovery runner received unsupported boundary contract")
        return registry

    def _run_bounded(
        self, store: Persistence, *, now: datetime, logical_now: datetime | None = None,
    ) -> int:
        actions = 0
        service = BoundaryService(store)
        resource_intents = (
            IntentResourceCoordinator(
                self.persistence, self.resource_policies,
                slot_duration=self.resource_slot_duration,
                retry_delay=self.resource_retry_delay,
                owner_epoch=getattr(getattr(store, "lease", None), "epoch", None),
                writer_scope=getattr(getattr(store, "lease", None), "scope", None),
            )
            if self.resource_policies is not None else None
        )
        # Reconcile a crash after immutable domain certification but before
        # release of its authoritative temporal reservation.
        if resource_intents is not None:
            owned_organizations = self.resource_organizations
            if self.correlation_id is not None:
                owned_organizations = {
                    self.correlation_id: self.resource_organizations[self.correlation_id],
                }
            resource_intents.reconcile_certified(store, owned_organizations)
        # A deferred effect stays durable, but must not monopolize this tick.
        deferred_this_tick: set[str] = set()
        for _ in range(self.max_actions):
            if actions >= self.max_actions:
                break
            pending = [
                pair for pair in self._pending_effects(store)
                if pair[1] not in deferred_this_tick
                and (self.correlation_id is None or pair[0].correlation_id == self.correlation_id)
            ]
            if pending:
                source, effect_id = pending[0]
                if resource_intents is not None:
                    command = store.command(effect_id)
                    if command is None:
                        raise RuntimeError("accepted effect disappeared before resource admission")
                    demand_due = command.due_at
                    if (
                        command.name == "composition.deliver_shipment"
                        and command.name in self.resource_policies
                        and store.entity("shipment", command.entity_id) is not None
                    ):
                        demand_due = customer.pickup_resource_due_at(store, command)
                    admitted = resource_intents.admit(
                        effect_id=effect_id,
                        intent_name=command.name,
                        organization_id=self.resource_organizations[source.correlation_id],
                        causation_id=source.message_id,
                        correlation_id=source.correlation_id,
                        due_at=demand_due,
                        now=logical_now if logical_now is not None else now,
                    )
                    if not admitted:
                        deferred_this_tick.add(effect_id)
                        continue
                pickup = (
                    resource_intents.booking_for(effect_id, command.name)
                    if resource_intents is not None
                    and command.name == "composition.deliver_shipment"
                    and command.name in self.resource_policies
                    else None
                )
                if pickup is not None:
                    self._execute_pending(
                        store, source, effect_id, authoritative_pickup=pickup,
                    )
                else:
                    self._execute_pending(store, source, effect_id)
                if resource_intents is not None:
                    resource_intents.complete(effect_id)
                actions += 1
                continue

            with store.transaction() as uow:
                before_ids = {
                    delivery.message_id for delivery in uow.boundary_deliveries()
                }
            reconstructed = customer.reconcile_shipped_fulfillment_egress(
                store, max_new_messages=self.max_actions - actions,
            **({"correlation_id": self.correlation_id} if self.correlation_id is not None else {}),
            )
            emitted = sum(message.message_id not in before_ids for message in reconstructed)
            if emitted:
                actions += emitted
                # Do not silently exceed the configured work budget.
                if actions >= self.max_actions:
                    break

            with store.transaction() as uow:
                intermediate_ids = {
                    delivery.message_id for delivery in uow.boundary_deliveries()
                }
            reconstructed_logistics = customer.reconcile_completed_logistics_egress(
                store, max_new_messages=self.max_actions - actions,
            **({"correlation_id": self.correlation_id} if self.correlation_id is not None else {}),
            )
            emitted_logistics = sum(
                message.message_id not in intermediate_ids
                for message in reconstructed_logistics
            )
            actions += emitted_logistics
            if actions >= self.max_actions:
                break

            with store.transaction() as uow:
                o2c_before_ids = {
                    delivery.message_id for delivery in uow.boundary_deliveries()
                }
            reconstructed_o2c = customer.reconcile_invoiced_o2c_egress(
                store, max_new_messages=self.max_actions - actions,
            **({"correlation_id": self.correlation_id} if self.correlation_id is not None else {}),
            )
            emitted_o2c = sum(
                message.message_id not in o2c_before_ids
                for message in reconstructed_o2c
            )
            actions += emitted_o2c
            if actions >= self.max_actions:
                break

            with store.transaction() as uow:
                payment_before_ids = {
                    delivery.message_id for delivery in uow.boundary_deliveries()
                }
            reconstructed_payment = customer.reconcile_settled_payment_egress(
                store, max_new_messages=self.max_actions - actions,
            **({"correlation_id": self.correlation_id} if self.correlation_id is not None else {}),
            )
            emitted_payment = sum(
                message.message_id not in payment_before_ids
                for message in reconstructed_payment
            )
            actions += emitted_payment
            if actions >= self.max_actions:
                break

            lease = service.claim_next(
                owner_id=self.owner_id,
                now=now,
                lease_duration=self.lease_duration,
                destination_domain="warehouse_management",
                accepted_contracts=frozenset({_WM_CONTRACT}),
                    correlation_id=self.correlation_id,
            )
            if lease is None:
                lease = service.claim_next(
                    owner_id=self.owner_id,
                    now=now,
                    lease_duration=self.lease_duration,
                    destination_domain="logistics",
                    accepted_contracts=frozenset({_LOGISTICS_CONTRACT}),
                    correlation_id=self.correlation_id,
                )
            if lease is None:
                lease = service.claim_next(
                    owner_id=self.owner_id,
                    now=now,
                    lease_duration=self.lease_duration,
                    destination_domain="order_to_cash",
                    accepted_contracts=frozenset({_O2C_CONTRACT}),
                    correlation_id=self.correlation_id,
                )
            if lease is None:
                lease = service.claim_next(
                    owner_id=self.owner_id,
                    now=now,
                    lease_duration=self.lease_duration,
                    destination_domain="cards_payments",
                    accepted_contracts=frozenset({_PAYMENTS_CONTRACT}),
                    correlation_id=self.correlation_id,
                )
            if lease is None:
                lease = service.claim_next(
                    owner_id=self.owner_id,
                    now=now,
                    lease_duration=self.lease_duration,
                    destination_domain="record_to_report",
                    accepted_contracts=frozenset({_R2R_CONTRACT}),
                    correlation_id=self.correlation_id,
                )
            if lease is None:
                if not emitted and not emitted_logistics and not emitted_o2c and not emitted_payment:
                    break
                continue

            with store.transaction() as uow:
                message = uow.get_boundary_message(lease.message_id)
            if message is None:
                raise RuntimeError("claimed WM boundary message disappeared")
            service.consume(
                lease=lease, registry=self._registry_for(message), now=now,
            )
            actions += 1
        return actions

    def run_scheduled_trigger(
        self, *, scheduled_for: datetime, observed_at: datetime | None = None,
    ) -> RecoveryTriggerResult:
        """Slot time defines identity; observed time governs physical leases."""
        from sose.jobs.runner import scheduled_trigger_id

        return self.run_trigger(
            trigger_id=scheduled_trigger_id(self.job_id, scheduled_for),
            now=scheduled_for, observed_at=observed_at,
        )

    def run_trigger(
        self, *, trigger_id: str, now: datetime, observed_at: datetime | None = None,
    ) -> RecoveryTriggerResult:
        if not trigger_id:
            raise ValueError("trigger_id must be nonempty")
        if self.scoped_writer:
            lease = self.persistence.claim_scoped_writer(
                self.job_id, self.owner_id,
                expected_epoch=self.persistence.scoped_writer_epoch(self.job_id),
            )
        else:
            lease = self.persistence.claim_writer(
                self.owner_id, expected_epoch=self.persistence.writer_epoch(),
            )
        store = FencedEnginePersistence(self.persistence, lease)
        with store.transaction() as uow:
            current = uow.get_job_state(self.job_id)
            if current is None:
                current = SimulationJobState(
                    job_id=self.job_id,
                    domain_name="trading_company_composition",
                    config_json="{}",
                    config_revision=1,
                    status="ready",
                    initialized=True,
                    logical_time=now,
                    next_tick=0,
                )
            if current.last_completed_trigger_id == trigger_id or any(
                record.trigger_id == trigger_id
                for record in current.completed_batch_triggers
            ):
                return RecoveryTriggerResult(self.job_id, trigger_id, 0)
            if now < current.logical_time:
                raise ValueError("composition trigger cannot rewind durable logical time")
            uow.save_job_state(replace(
                current,
                status="running",
                phase="reconcile",
                active_trigger_id=trigger_id,
                logical_time=now,
                last_triggered_at=now,
            ))

        lease_clock = observed_at if observed_at is not None else now
        if self.resource_policies is None:
            # Legacy recovery extension points accept only 'now'. Preserve
            # their call contract and the historical physical lease clock.
            actions = self._run_bounded(store, now=lease_clock)
        else:
            actions = self._run_bounded(store, now=lease_clock, logical_now=now)
        with store.transaction() as uow:
            current = uow.get_job_state(self.job_id)
            if current is None or current.active_trigger_id != trigger_id:
                raise RuntimeError("composition recovery trigger ownership lost")
            completed_record = CompletedJobTrigger(
                trigger_id=trigger_id,
                requested_ticks=1,
                start_tick=current.next_tick,
                end_tick=current.next_tick + 1,
                config_revision=current.config_revision,
                logical_time=now,
                run_count=current.run_count + 1,
            )
            uow.save_job_state(replace(
                current,
                status="ready",
                phase="idle",
                active_trigger_id=None,
                last_completed_trigger_id=trigger_id,
                completed_batch_triggers=(
                    *current.completed_batch_triggers, completed_record,
                ),
                next_tick=current.next_tick + 1,
                run_count=current.run_count + 1,
                last_error=None,
            ))
        return RecoveryTriggerResult(self.job_id, trigger_id, actions)

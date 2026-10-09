"""Bounded, restartable PC6 customer-boundary recovery trigger.

This runner owns no business entities. It replays accepted domain intents,
reconstructs outbound WF messages from durable facts, and applies WM consumption
through the existing boundary registry. It now resumes Logistics effects from accepted dispatches. O2C/Payments/R2R
still require separate domain worker ownership.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timedelta

from sose.composition import trading_company_customer as customer
from sose.composition.boundary import BoundaryConsumerRegistry, BoundaryService
from sose.composition.model import BoundaryMessage, DeliveryStatus
from sose.examples.warehouse_fulfillment import simulation as fulfillment
from sose.examples.logistics import simulation as logistics
from sose.examples.order_to_cash import simulation as o2c
from sose.jobs.model import CompletedJobTrigger, SimulationJobState
from sose.persistence.base import Persistence
from sose.persistence.ownership import FencedEnginePersistence


_WM_CONTRACT = ("warehouse.inventory_consumption_requested", 1)
_LOGISTICS_CONTRACT = ("warehouse.dispatch_ready", 1)
_O2C_CONTRACT = ("logistics.delivery_completed", 1)
_RECOVERABLE = frozenset({
    "composition.accept_inventory_reservation",
    "composition.consume_fulfillment_inventory",
    "composition.deliver_shipment",
    "composition.complete_external_fulfillment",
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

    def __post_init__(self) -> None:
        if not self.owner_id or not self.job_id:
            raise ValueError("worker owner and job identity must be nonempty")
        if self.max_actions < 1:
            raise ValueError("max_actions must be >= 1")
        if self.lease_duration <= timedelta(0):
            raise ValueError("lease_duration must be positive")
        if not callable(getattr(self.persistence, "writer_epoch", None)) or not callable(
            getattr(self.persistence, "claim_writer", None)
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
    ) -> None:
        details = source.payload()
        order_id = str(details.get("fulfillment_order_id", ""))
        sales_order_id = str(details.get("order_id", ""))
        shipment_id = str(details.get("shipment_id", ""))
        fixture = customer._CustomerFixtures(
            o2c=o2c.O2CEntities(order_id=sales_order_id),
            fulfillment=fulfillment.WarehouseEntities(order_id=order_id, lot_ids=()),
            warehouse=None,
            logistics=logistics.LogisticsEntities(shipment_id=shipment_id),
            payments=None,
            r2r=None,
        )
        customer._execute_intent(
            store,
            effect_id=effect_id,
            fixtures=fixture,
            correlation_id=source.correlation_id,
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
        else:
            raise ValueError("recovery runner received unsupported boundary contract")
        return registry

    def _run_bounded(self, store: Persistence, *, now: datetime) -> int:
        actions = 0
        service = BoundaryService(store)
        for _ in range(self.max_actions):
            if actions >= self.max_actions:
                break
            pending = self._pending_effects(store)
            if pending:
                source, effect_id = pending[0]
                self._execute_pending(store, source, effect_id)
                actions += 1
                continue

            with store.transaction() as uow:
                before_ids = {
                    delivery.message_id for delivery in uow.boundary_deliveries()
                }
            reconstructed = customer.reconcile_shipped_fulfillment_egress(
                store, max_new_messages=self.max_actions - actions,
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
            )
            emitted_o2c = sum(
                message.message_id not in o2c_before_ids
                for message in reconstructed_o2c
            )
            actions += emitted_o2c
            if actions >= self.max_actions:
                break

            lease = service.claim_next(
                owner_id=self.owner_id,
                now=now,
                lease_duration=self.lease_duration,
                destination_domain="warehouse_management",
                accepted_contracts=frozenset({_WM_CONTRACT}),
            )
            if lease is None:
                lease = service.claim_next(
                    owner_id=self.owner_id,
                    now=now,
                    lease_duration=self.lease_duration,
                    destination_domain="logistics",
                    accepted_contracts=frozenset({_LOGISTICS_CONTRACT}),
                )
            if lease is None:
                lease = service.claim_next(
                    owner_id=self.owner_id,
                    now=now,
                    lease_duration=self.lease_duration,
                    destination_domain="order_to_cash",
                    accepted_contracts=frozenset({_O2C_CONTRACT}),
                )
            if lease is None:
                if not emitted and not emitted_logistics and not emitted_o2c:
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

        actions = self._run_bounded(store, now=observed_at if observed_at is not None else now)
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

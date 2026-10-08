from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta

from sose.backends.simpy import SimPyBackend
from sose.core.events import Command
from sose.core.identity import deterministic_id
from sose.persistence.memory import MemoryPersistence

from sose.examples.p2p import simulation as p2p
from sose.examples.warehouse_management import simulation as wm
from sose.examples.record_to_report import simulation as r2r

from .boundary import BoundaryConsumerRegistry, BoundaryService
from .model import BoundaryMessage


@dataclass(frozen=True, slots=True)
class ReplenishmentPathResult:
    persistence: MemoryPersistence
    correlation_id: str
    stock_id: str
    purchase_order_id: str
    receipt_id: str
    journal_id: str
    message_ids: tuple[str, ...]
    effect_ids: tuple[str, ...]
    receipt_replay_was_idempotent: bool


@dataclass(frozen=True, slots=True)
class _Fixtures:
    warehouse: wm.WarehouseManagementEntities
    procurement: p2p.P2PEntities
    accounting: r2r.R2REntities


class _CausalCommandFactory:
    def __init__(self, delegate, *, correlation_id: str, caused_by) -> None:
        self._delegate = delegate
        self._correlation_id = correlation_id
        self._caused_by = caused_by

    def create(self, name: str, **kwargs):
        kwargs["correlation_id"] = self._correlation_id
        kwargs["caused_by"] = self._caused_by
        return self._delegate.create(name, **kwargs)


@contextmanager
def _causal_scope(engine, *, correlation_id: str, caused_by):
    original = engine.context.commands
    engine.context.commands = _CausalCommandFactory(
        original,
        correlation_id=correlation_id,
        caused_by=caused_by,
    )
    try:
        yield
    finally:
        engine.context.commands = original


def _intent_handler(*, name: str, entity_type: str, entity_id: str):
    def handle(message: BoundaryMessage, uow) -> str:
        command_id = deterministic_id(
            "composition-domain-intent",
            message.message_id,
            name,
            entity_type,
            entity_id,
        )
        command = Command(
            command_id=command_id,
            name=name,
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


def _registry(fixtures: _Fixtures) -> BoundaryConsumerRegistry:
    registry = BoundaryConsumerRegistry()
    registry.register(
        destination_domain="procure_to_pay",
        contract_name="warehouse.replenishment_requested",
        contract_version=1,
        handler=_intent_handler(
            name="composition.procure_replenishment",
            entity_type="purchase_order",
            entity_id=fixtures.procurement.purchase_order_id,
        ),
    )
    registry.register(
        destination_domain="warehouse_management",
        contract_name="p2p.inventory_receipt_ready",
        contract_version=1,
        handler=_intent_handler(
            name="composition.receive_replenishment",
            entity_type="warehouse_management_stock",
            entity_id=fixtures.warehouse.origin_stock_id,
        ),
    )
    registry.register(
        destination_domain="record_to_report",
        contract_name="accounting.entry_requested",
        contract_version=1,
        handler=_intent_handler(
            name="composition.post_replenishment_journal",
            entity_type="journal_entry",
            entity_id=fixtures.accounting.journal_id,
        ),
    )
    return registry


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


def _claim_consume(
    service: BoundaryService,
    registry: BoundaryConsumerRegistry,
    *,
    owner_id: str,
    now: datetime,
):
    lease = service.claim_next(
        owner_id=owner_id,
        now=now,
        lease_duration=timedelta(hours=1),
    )
    if lease is None:
        raise RuntimeError("expected one pending replenishment boundary delivery")
    consumption = service.consume(lease=lease, registry=registry, now=now)
    return lease, consumption


def _recovery_time(store: MemoryPersistence, requested: datetime) -> datetime:
    position = store.simulation_position()
    if position is None:
        return requested
    if requested < position.logical_time:
        raise ValueError("replenishment intent precedes committed logical time")
    return position.logical_time


def _next_time(store: MemoryPersistence, previous: datetime) -> datetime:
    candidate = previous + timedelta(minutes=1)
    position = store.simulation_position()
    if position is not None and position.logical_time > candidate:
        return position.logical_time
    return candidate


def _delete_intent(store: MemoryPersistence, effect_id: str, expected: Command) -> None:
    with store.transaction() as uow:
        current = uow.get_command(effect_id)
        if current == expected:
            uow.delete_command(effect_id)


def _execute_procurement(
    store: MemoryPersistence,
    *,
    intent: Command,
    fixtures: _Fixtures,
    quantity: float,
    correlation_id: str,
) -> None:
    recovery = _recovery_time(store, intent.due_at)
    _, engine = p2p.build_runtime(store, now=recovery)
    backend = SimPyBackend(origin=recovery)

    p2p.schedule_procurement_cycle(
        store,
        engine,
        entities=fixtures.procurement,
        start_at=intent.due_at,
        correlation_id=correlation_id,
        caused_by=intent,
    )
    engine.rebuild_backend(backend)
    backend.run_until(intent.due_at + timedelta(hours=10))
    with _causal_scope(
        engine,
        correlation_id=correlation_id,
        caused_by=intent,
    ):
        if not p2p.reconcile_receiving_resources(
            store,
            engine,
            backend,
            entities=fixtures.procurement,
        ):
            raise RuntimeError("P2P receiving capacity unavailable")

        backend.run_until(intent.due_at + timedelta(hours=11))
        p2p.reconcile_stocking(
            store,
            engine,
            backend,
            entities=fixtures.procurement,
            quantity=quantity,
        )

    receipt = store.entity("receipt", fixtures.procurement.receipt_id)
    if receipt is None or receipt.state != "stocked":
        raise RuntimeError("P2P replenishment receipt did not reach stocked")

    _delete_intent(store, intent.command_id, intent)


def _execute_warehouse_receipt(
    store: MemoryPersistence,
    *,
    intent: Command,
    fixtures: _Fixtures,
    quantity: float,
    correlation_id: str,
) -> None:
    recovery = _recovery_time(store, intent.due_at)
    _, engine = wm.build_runtime(store, now=recovery)
    if intent.due_at > engine.context.clock.now:
        engine.context.clock.now = intent.due_at

    wm.receive_external_replenishment(
        store,
        engine,
        stock_id=fixtures.warehouse.origin_stock_id,
        quantity=quantity,
        receipt_reference=fixtures.procurement.receipt_id,
        caused_by=intent,
        correlation_id=correlation_id,
    )
    _delete_intent(store, intent.command_id, intent)


def _release_p2p_staging(
    store: MemoryPersistence,
    *,
    caused_by: Command,
    fixtures: _Fixtures,
    quantity: float,
    correlation_id: str,
) -> None:
    recovery = store.simulation_position()
    now = caused_by.due_at if recovery is None else recovery.logical_time
    _, engine = p2p.build_runtime(store, now=now)
    backend = SimPyBackend(origin=now)
    engine.rebuild_backend(backend)
    with _causal_scope(
        engine,
        correlation_id=correlation_id,
        caused_by=caused_by,
    ):
        p2p.reconcile_consumption(
            store,
            engine,
            backend,
            entities=fixtures.procurement,
            quantity=quantity,
        )


def _execute_accounting(
    store: MemoryPersistence,
    *,
    intent: Command,
    fixtures: _Fixtures,
    correlation_id: str,
) -> None:
    recovery = _recovery_time(store, intent.due_at)
    _, engine = r2r.build_runtime(store, now=recovery)
    backend = SimPyBackend(origin=recovery)
    engine.rebuild_backend(backend)
    if backend.now < intent.due_at:
        backend.run_until(intent.due_at)
    with _causal_scope(
        engine,
        correlation_id=correlation_id,
        caused_by=intent,
    ):
        if not r2r.submit_and_post_journal(
            store,
            engine,
            backend,
            entities=fixtures.accounting,
        ):
            raise RuntimeError("replenishment accounting journal failed")
    _delete_intent(store, intent.command_id, intent)


def run_replenishment_path(*, quantity: float = 5.0) -> ReplenishmentPathResult:
    if quantity <= 0:
        raise ValueError("quantity must be positive")

    store = MemoryPersistence()
    origin = p2p.ORIGIN
    amount = 500.0
    currency = "USD"
    fixtures = _Fixtures(
        warehouse=wm.seed_reference(
            store,
            now=origin,
            origin_on_hand=2.0,
            transfer_quantity=1.0,
        ),
        procurement=p2p.seed_happy_path(
            store,
            now=origin,
            quantity=quantity,
            schedule=False,
        ),
        accounting=r2r.seed_reference(
            store,
            now=origin,
            amount=amount,
            currency=currency,
        ),
    )

    correlation_id = deterministic_id(
        "trading-company-replenishment",
        fixtures.warehouse.origin_stock_id,
        fixtures.procurement.purchase_order_id,
    )
    service = BoundaryService(store)
    registry = _registry(fixtures)
    messages: list[BoundaryMessage] = []
    effects: list[str] = []

    message = _publish(
        service,
        contract_name="warehouse.replenishment_requested",
        source_domain="warehouse_management",
        source_identity=fixtures.warehouse.origin_stock_id,
        destination_domain="procure_to_pay",
        occurrence_key="replenishment-requested",
        correlation_id=correlation_id,
        causation_id=None,
        produced_at=origin,
        payload={
            "stock_id": fixtures.warehouse.origin_stock_id,
            "purchase_order_id": fixtures.procurement.purchase_order_id,
            "sku": p2p.SKU,
            "quantity": quantity,
        },
    )
    messages.append(message)
    _, consumption = _claim_consume(
        service,
        registry,
        owner_id="p2p-worker",
        now=message.produced_at,
    )
    effects.append(consumption.consumer_effect_id)
    intent = store.command(consumption.consumer_effect_id)
    if intent is None:
        raise RuntimeError("P2P replenishment intent was not persisted")
    _execute_procurement(
        store,
        intent=intent,
        fixtures=fixtures,
        quantity=quantity,
        correlation_id=correlation_id,
    )

    message = _publish(
        service,
        contract_name="p2p.inventory_receipt_ready",
        source_domain="procure_to_pay",
        source_identity=fixtures.procurement.receipt_id,
        destination_domain="warehouse_management",
        occurrence_key="inventory-receipt-ready",
        correlation_id=correlation_id,
        causation_id=messages[-1].message_id,
        produced_at=_next_time(store, messages[-1].produced_at),
        payload={
            "receipt_id": fixtures.procurement.receipt_id,
            "stock_id": fixtures.warehouse.origin_stock_id,
            "sku": p2p.SKU,
            "quantity": quantity,
        },
    )
    messages.append(message)
    lease, consumption = _claim_consume(
        service,
        registry,
        owner_id="warehouse-worker",
        now=message.produced_at,
    )
    effects.append(consumption.consumer_effect_id)
    intent = store.command(consumption.consumer_effect_id)
    if intent is None:
        raise RuntimeError("WM replenishment intent was not persisted")
    _execute_warehouse_receipt(
        store,
        intent=intent,
        fixtures=fixtures,
        quantity=quantity,
        correlation_id=correlation_id,
    )

    replay = service.consume(
        lease=lease,
        registry=registry,
        now=message.produced_at,
    )
    receipt_replay_was_idempotent = replay == consumption

    _release_p2p_staging(
        store,
        caused_by=intent,
        fixtures=fixtures,
        quantity=quantity,
        correlation_id=correlation_id,
    )

    message = _publish(
        service,
        contract_name="accounting.entry_requested",
        source_domain="warehouse_management",
        source_identity=fixtures.warehouse.origin_stock_id,
        destination_domain="record_to_report",
        occurrence_key="replenishment-accounting-entry",
        correlation_id=correlation_id,
        causation_id=messages[-1].message_id,
        produced_at=_next_time(store, messages[-1].produced_at),
        payload={
            "receipt_id": fixtures.procurement.receipt_id,
            "journal_id": fixtures.accounting.journal_id,
            "amount": amount,
            "currency": currency,
        },
    )
    messages.append(message)
    _, consumption = _claim_consume(
        service,
        registry,
        owner_id="r2r-worker",
        now=message.produced_at,
    )
    effects.append(consumption.consumer_effect_id)
    intent = store.command(consumption.consumer_effect_id)
    if intent is None:
        raise RuntimeError("R2R replenishment intent was not persisted")
    _execute_accounting(
        store,
        intent=intent,
        fixtures=fixtures,
        correlation_id=correlation_id,
    )

    return ReplenishmentPathResult(
        persistence=store,
        correlation_id=correlation_id,
        stock_id=fixtures.warehouse.origin_stock_id,
        purchase_order_id=fixtures.procurement.purchase_order_id,
        receipt_id=fixtures.procurement.receipt_id,
        journal_id=fixtures.accounting.journal_id,
        message_ids=tuple(message.message_id for message in messages),
        effect_ids=tuple(effects),
        receipt_replay_was_idempotent=receipt_replay_was_idempotent,
    )


__all__ = ["ReplenishmentPathResult", "run_replenishment_path"]

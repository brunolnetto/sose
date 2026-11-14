"""Replenishment intent executors and their helpers, split from trading_company_replenishment."""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta

from sose.backends.simpy import SimPyBackend
from sose.core.events import Command
from sose.persistence.memory import MemoryPersistence

from sose.examples.p2p import simulation as p2p
from sose.examples.warehouse_management import simulation as wm
from sose.examples.record_to_report import simulation as r2r

from .effects import BusinessEffectService


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


def _recovery_time(store: MemoryPersistence, requested: datetime, *, owner_id: str) -> datetime:
    position = store.simulation_position(owner_id=owner_id)
    if position is None:
        return requested
    if requested < position.logical_time:
        raise ValueError("replenishment intent precedes committed logical time")
    return position.logical_time


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
    owner_id: str,
) -> None:
    recovery = _recovery_time(store, intent.due_at, owner_id=owner_id)
    _, engine = p2p.build_runtime(store, owner_id=owner_id, now=recovery)
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
    owner_id: str,
) -> None:
    recovery = _recovery_time(store, intent.due_at, owner_id=owner_id)
    _, engine = wm.build_runtime(store, owner_id=owner_id, now=recovery)
    if intent.due_at > engine.context.clock.now:
        engine.context.clock.now = intent.due_at

    wm.receive_external_replenishment(
        store,
        engine,
        stock_id=fixtures.warehouse.origin_stock_id,
        quantity=quantity,
        sku=p2p.SKU,
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
    owner_id: str,
) -> None:
    recovery = store.simulation_position(owner_id=owner_id)
    now = caused_by.due_at if recovery is None else recovery.logical_time
    _, engine = p2p.build_runtime(store, owner_id=owner_id, now=now)
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
    owner_id: str,
) -> None:
    recovery = _recovery_time(store, intent.due_at, owner_id=owner_id)
    _, engine = r2r.build_runtime(store, owner_id=owner_id, now=recovery)
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
    # The same R2R contract is used by customer settlements and replenishment.
    # Both must produce an immutable business-completion receipt, distinct
    # from a boundary transport ACK.
    position = store.simulation_position(owner_id=owner_id)
    completed_at = max(
        intent.due_at,
        position.logical_time if position is not None else intent.due_at,
    )
    BusinessEffectService(store).complete(
        effect_id=intent.command_id, completed_at=completed_at,
    )

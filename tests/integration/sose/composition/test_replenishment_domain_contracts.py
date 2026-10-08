from __future__ import annotations

from datetime import timedelta

import pytest

from sose.core.events import Command
from sose.examples.p2p.simulation import (
    ORIGIN as P2P_ORIGIN,
    SKU as P2P_SKU,
    build_runtime as build_p2p_runtime,
    schedule_procurement_cycle,
    seed_happy_path,
)
from sose.examples.warehouse_management.simulation import (
    ORIGIN as WM_ORIGIN,
    build_runtime as build_wm_runtime,
    receive_external_replenishment,
    seed_reference,
)
from sose.persistence.memory import MemoryPersistence


def test_p2p_procurement_schedule_can_inherit_external_causality() -> None:
    store = MemoryPersistence()
    entities = seed_happy_path(
        store,
        now=P2P_ORIGIN,
        quantity=5.0,
        schedule=False,
    )
    assert store.scheduled_work() == ()

    _, engine = build_p2p_runtime(store, now=P2P_ORIGIN)
    cause = Command(
        command_id="boundary-intent",
        name="composition.replenish",
        entity_type="purchase_order",
        entity_id=entities.purchase_order_id,
        due_at=P2P_ORIGIN,
        correlation_id="composition-correlation",
    )
    schedule_procurement_cycle(
        store,
        engine,
        entities=entities,
        start_at=P2P_ORIGIN,
        correlation_id="composition-correlation",
        caused_by=cause,
    )

    commands = tuple(
        store.command(work.command_id)
        for work in store.scheduled_work()
        if store.command(work.command_id) is not None
    )
    assert commands
    assert {command.correlation_id for command in commands} == {
        "composition-correlation"
    }
    first = min(commands, key=lambda command: command.due_at)
    assert first.causation_id == cause.command_id
    assert max(command.due_at for command in commands) == P2P_ORIGIN + timedelta(hours=11)


def test_warehouse_external_replenishment_is_idempotent_and_conflict_safe() -> None:
    store = MemoryPersistence()
    entities = seed_reference(
        store,
        now=WM_ORIGIN,
        origin_on_hand=2.0,
        transfer_quantity=1.0,
        sku=P2P_SKU,
    )
    _, engine = build_wm_runtime(store, now=WM_ORIGIN)
    cause = Command(
        command_id="p2p-receipt-intent",
        name="composition.receive-replenishment",
        entity_type="warehouse_management_stock",
        entity_id=entities.origin_stock_id,
        due_at=WM_ORIGIN,
        correlation_id="replenishment-flow",
    )

    assert receive_external_replenishment(
        store,
        engine,
        stock_id=entities.origin_stock_id,
        quantity=5.0,
        sku=P2P_SKU,
        receipt_reference="receipt-42",
        caused_by=cause,
        correlation_id="replenishment-flow",
    ) is True
    assert receive_external_replenishment(
        store,
        engine,
        stock_id=entities.origin_stock_id,
        quantity=5.0,
        sku=P2P_SKU,
        receipt_reference="receipt-42",
        caused_by=cause,
        correlation_id="replenishment-flow",
    ) is False

    stock = store.entity("warehouse_management_stock", entities.origin_stock_id)
    assert stock is not None
    assert stock.attributes["on_hand"] == 7.0
    assert stock.attributes["external_receipts"] == {"receipt-42": 5.0}

    event = next(
        event
        for event in store.events()
        if event.name == "warehouse_management.replenishment_received"
    )
    assert event.causation_id == cause.command_id
    assert event.correlation_id == "replenishment-flow"

    with pytest.raises(ValueError, match="receipt replay conflict"):
        receive_external_replenishment(
            store,
            engine,
            stock_id=entities.origin_stock_id,
            quantity=6.0,
            sku=P2P_SKU,
            receipt_reference="receipt-42",
            caused_by=cause,
            correlation_id="replenishment-flow",
        )


def test_warehouse_replenishment_rejects_sku_mismatch() -> None:
    store = MemoryPersistence()
    entities = seed_reference(
        store,
        now=WM_ORIGIN,
        origin_on_hand=2.0,
        transfer_quantity=1.0,
        sku=P2P_SKU,
    )
    _, engine = build_wm_runtime(store, now=WM_ORIGIN)

    with pytest.raises(ValueError, match="replenishment SKU mismatch"):
        receive_external_replenishment(
            store,
            engine,
            stock_id=entities.origin_stock_id,
            quantity=5.0,
            sku="different-sku",
            receipt_reference="receipt-mismatch",
            correlation_id="replenishment-flow",
        )

    stock = store.entity("warehouse_management_stock", entities.origin_stock_id)
    assert stock.attributes["on_hand"] == 2.0

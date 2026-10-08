from __future__ import annotations

from dataclasses import replace
from datetime import timedelta

import pytest

from sose.composition.boundary import (
    BoundaryConsumerRegistry,
    BoundaryService,
    DeliveryStatus,
    StaleBoundaryClaimError,
)
from sose.composition import trading_company_customer as customer
from sose.composition import trading_company_replenishment as replenishment
from sose.core.identity import deterministic_id
from sose.examples.cards_payments import simulation as payments
from sose.examples.logistics import simulation as logistics
from sose.examples.order_to_cash import simulation as o2c
from sose.examples.p2p import simulation as p2p
from sose.examples.record_to_report import simulation as r2r
from sose.examples.warehouse_fulfillment import simulation as fulfillment
from sose.examples.warehouse_management import simulation as wm
from sose.persistence.sqlite import SQLitePersistence


def _reopen(store: SQLitePersistence, path) -> SQLitePersistence:
    store.close()
    return SQLitePersistence(path)


def _boundary_semantics(store) -> tuple[tuple[object, ...], ...]:
    with store.transaction() as uow:
        deliveries = tuple(
            sorted(
                uow.boundary_deliveries(),
                key=lambda delivery: (
                    uow.get_boundary_message(delivery.message_id).produced_at,
                    delivery.delivery_id,
                ),
            )
        )
        rows: list[tuple[object, ...]] = []
        for delivery in deliveries:
            message = uow.get_boundary_message(delivery.message_id)
            consumption = uow.get_boundary_consumption(delivery.delivery_id)
            rows.append(
                (
                    message,
                    delivery.status,
                    None if consumption is None else consumption.consumer_effect_id,
                )
            )
        return tuple(rows)


def _claim_after_worker_death(
    store: SQLitePersistence,
    path,
    *,
    registry_factory,
    owner_id: str,
    now,
):
    service = BoundaryService(store)
    first = service.claim_next(
        owner_id=owner_id,
        now=now,
        lease_duration=timedelta(seconds=5),
    )
    assert first is not None

    store = _reopen(store, path)
    service = BoundaryService(store)
    second = service.claim_next(
        owner_id=owner_id,
        now=now + timedelta(seconds=6),
        lease_duration=timedelta(hours=1),
    )
    assert second is not None
    assert second.delivery_id == first.delivery_id
    assert second.owner_id == first.owner_id
    assert second.epoch == first.epoch + 1

    registry = registry_factory()
    stale_epoch = replace(first, lease_expires_at=second.lease_expires_at)
    assert stale_epoch.owner_id == second.owner_id
    assert stale_epoch.lease_expires_at == second.lease_expires_at
    with pytest.raises(StaleBoundaryClaimError):
        service.consume(
            lease=stale_epoch,
            registry=registry,
            now=now + timedelta(seconds=7),
        )

    consumption = service.consume(
        lease=second,
        registry=registry,
        now=now + timedelta(seconds=7),
    )
    assert service.delivery(second.delivery_id).status is DeliveryStatus.CONSUMED
    return store, second, consumption


def _customer_fixtures(store) -> customer._CustomerFixtures:
    origin = o2c.ORIGIN
    return customer._CustomerFixtures(
        o2c=o2c.seed_reference(store, now=origin, amount=250.0, currency="USD"),
        fulfillment=fulfillment.seed_composed_reference(
            store,
            now=origin,
            requested_quantity=10.0,
        ),
        warehouse=wm.seed_reference(
            store,
            now=origin,
            origin_on_hand=20.0,
            transfer_quantity=1.0,
            sku=fulfillment.PRIMARY_SKU,
        ),
        logistics=logistics.seed_reference(store, now=origin),
        payments=payments.seed_reference(
            store,
            now=origin,
            amount=250.0,
            currency="USD",
        ),
        r2r=r2r.seed_reference(
            store,
            now=origin,
            amount=250.0,
            currency="USD",
        ),
    )


def _customer_business_state(store, fixtures: customer._CustomerFixtures):
    local_lots = tuple(
        entity
        for entity in store.entities()
        if entity.entity_type == "warehouse_inventory_lot"
    )
    return (
        store.entity("sales_order", fixtures.o2c.order_id),
        store.entity("warehouse_fulfillment_order", fixtures.fulfillment.order_id),
        store.entity(
            "warehouse_management_stock",
            fixtures.warehouse.origin_stock_id,
        ),
        store.entity("shipment", fixtures.logistics.shipment_id),
        store.entity("card_payment", fixtures.payments.payment_id),
        store.entity("journal_entry", fixtures.r2r.journal_id),
        local_lots,
    )


def test_customer_path_is_continuous_equivalent_across_worker_death_and_restart(
    tmp_path,
) -> None:
    continuous = customer.run_customer_demand_path()
    path = tmp_path / "customer-composition.sqlite3"
    store = SQLitePersistence(path)
    fixtures = _customer_fixtures(store)
    origin = o2c.ORIGIN

    _, engine = o2c.build_runtime(store, now=origin)
    assert o2c.reconcile_credit(store, engine, entities=fixtures.o2c)

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
    messages = []
    effects = []

    stages = (
        {
            "contract_name": "o2c.fulfillment_requested",
            "source_domain": "order_to_cash",
            "source_identity": fixtures.o2c.order_id,
            "destination_domain": "warehouse_fulfillment",
            "occurrence_key": "fulfillment-requested",
            "owner_id": "warehouse-fulfillment-worker",
            "payload": {
                "order_id": fixtures.o2c.order_id,
                "fulfillment_order_id": fixtures.fulfillment.order_id,
                "requested_quantity": 10.0,
                "sku": fulfillment.PRIMARY_SKU,
            },
        },
        {
            "contract_name": "warehouse.inventory_reservation_requested",
            "source_domain": "warehouse_fulfillment",
            "source_identity": fixtures.fulfillment.order_id,
            "destination_domain": "warehouse_management",
            "occurrence_key": "inventory-reservation-requested",
            "owner_id": "warehouse-management-worker",
            "payload": {
                "fulfillment_order_id": fixtures.fulfillment.order_id,
                "stock_id": fixtures.warehouse.origin_stock_id,
                "reservation_reference": reservation_reference,
                "sku": fulfillment.PRIMARY_SKU,
                "quantity": 10.0,
            },
        },
        {
            "contract_name": "warehouse.inventory_reserved",
            "source_domain": "warehouse_management",
            "source_identity": fixtures.warehouse.origin_stock_id,
            "destination_domain": "warehouse_fulfillment",
            "occurrence_key": "inventory-reserved",
            "owner_id": "warehouse-fulfillment-worker",
            "payload": {
                "fulfillment_order_id": fixtures.fulfillment.order_id,
                "stock_id": fixtures.warehouse.origin_stock_id,
                "reservation_reference": reservation_reference,
                "sku": fulfillment.PRIMARY_SKU,
                "quantity": 10.0,
            },
        },
        {
            "contract_name": "warehouse.inventory_consumption_requested",
            "source_domain": "warehouse_fulfillment",
            "source_identity": fixtures.fulfillment.order_id,
            "destination_domain": "warehouse_management",
            "occurrence_key": "inventory-consumption-requested",
            "owner_id": "warehouse-management-worker",
            "payload": {
                "fulfillment_order_id": fixtures.fulfillment.order_id,
                "stock_id": fixtures.warehouse.origin_stock_id,
                "reservation_reference": reservation_reference,
                "consumption_reference": consumption_reference,
                "sku": fulfillment.PRIMARY_SKU,
                "quantity": 10.0,
            },
        },
        {
            "contract_name": "warehouse.dispatch_ready",
            "source_domain": "warehouse_fulfillment",
            "source_identity": fixtures.fulfillment.order_id,
            "destination_domain": "logistics",
            "occurrence_key": "dispatch-ready",
            "owner_id": "logistics-worker",
            "payload": {
                "fulfillment_order_id": fixtures.fulfillment.order_id,
                "shipment_id": fixtures.logistics.shipment_id,
            },
        },
        {
            "contract_name": "logistics.delivery_completed",
            "source_domain": "logistics",
            "source_identity": fixtures.logistics.shipment_id,
            "destination_domain": "order_to_cash",
            "occurrence_key": "delivery-completed",
            "owner_id": "o2c-worker",
            "payload": {
                "shipment_id": fixtures.logistics.shipment_id,
                "order_id": fixtures.o2c.order_id,
            },
        },
        {
            "contract_name": "o2c.payment_requested",
            "source_domain": "order_to_cash",
            "source_identity": fixtures.o2c.order_id,
            "destination_domain": "cards_payments",
            "occurrence_key": "payment-requested",
            "owner_id": "payments-worker",
            "payload": {
                "order_id": fixtures.o2c.order_id,
                "payment_id": fixtures.payments.payment_id,
                "amount": 250.0,
                "currency": "USD",
            },
        },
        {
            "contract_name": "accounting.entry_requested",
            "source_domain": "cards_payments",
            "source_identity": fixtures.payments.payment_id,
            "destination_domain": "record_to_report",
            "occurrence_key": "customer-settlement-entry",
            "owner_id": "r2r-worker",
            "payload": {
                "payment_id": fixtures.payments.payment_id,
                "journal_id": fixtures.r2r.journal_id,
                "amount": 250.0,
                "currency": "USD",
            },
        },
    )

    produced_at = origin
    causation_id = None
    for stage in stages:
        service = BoundaryService(store)
        message = customer._publish(
            service,
            contract_name=stage["contract_name"],
            source_domain=stage["source_domain"],
            source_identity=stage["source_identity"],
            destination_domain=stage["destination_domain"],
            occurrence_key=stage["occurrence_key"],
            correlation_id=correlation_id,
            causation_id=causation_id,
            produced_at=produced_at,
            payload=stage["payload"],
        )
        messages.append(message)

        store, lease, consumption = _claim_after_worker_death(
            store,
            path,
            registry_factory=lambda: customer._registry(fixtures),
            owner_id=stage["owner_id"],
            now=message.produced_at,
        )
        effects.append(consumption.consumer_effect_id)

        customer._execute_intent(
            store,
            effect_id=consumption.consumer_effect_id,
            fixtures=fixtures,
            correlation_id=correlation_id,
        )
        store = _reopen(store, path)

        replay = BoundaryService(store).consume(
            lease=lease,
            registry=customer._registry(fixtures),
            now=message.produced_at + timedelta(minutes=1),
        )
        assert replay.consumer_effect_id == consumption.consumer_effect_id

        causation_id = message.message_id
        produced_at = customer._next_logical_time(store, message.produced_at)

    assert tuple(message.message_id for message in messages) == continuous.message_ids
    assert tuple(effects) == continuous.effect_ids
    assert reservation_reference == continuous.reservation_reference
    assert consumption_reference == continuous.consumption_reference
    assert _customer_business_state(store, fixtures) == (
        continuous.persistence.entity("sales_order", continuous.o2c_order_id),
        continuous.persistence.entity(
            "warehouse_fulfillment_order",
            continuous.fulfillment_order_id,
        ),
        continuous.persistence.entity(
            "warehouse_management_stock",
            continuous.warehouse_stock_id,
        ),
        continuous.persistence.entity("shipment", continuous.shipment_id),
        continuous.persistence.entity("card_payment", continuous.payment_id),
        continuous.persistence.entity("journal_entry", continuous.journal_id),
        (),
    )

    rebuilt_semantics = _boundary_semantics(store)
    continuous_semantics = _boundary_semantics(continuous.persistence)
    assert rebuilt_semantics == continuous_semantics

    with store.transaction() as uow:
        assert all(
            delivery.attempts == 2 and delivery.claim_epoch == 2
            for delivery in uow.boundary_deliveries()
        )
    store.close()


def _replenishment_fixtures(store, *, quantity: float) -> replenishment._Fixtures:
    origin = p2p.ORIGIN
    return replenishment._Fixtures(
        warehouse=wm.seed_reference(
            store,
            now=origin,
            origin_on_hand=2.0,
            transfer_quantity=1.0,
            sku=p2p.SKU,
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
            amount=500.0,
            currency="USD",
        ),
    )


def _replenishment_business_state(store, fixtures: replenishment._Fixtures):
    return (
        store.entity(
            "warehouse_management_stock",
            fixtures.warehouse.origin_stock_id,
        ),
        store.entity("purchase_order", fixtures.procurement.purchase_order_id),
        store.entity("receipt", fixtures.procurement.receipt_id),
        store.entity("journal_entry", fixtures.accounting.journal_id),
        tuple(store.container_states()),
    )


def test_replenishment_path_is_continuous_equivalent_across_restart_and_fencing(
    tmp_path,
) -> None:
    quantity = 5.0
    continuous = replenishment.run_replenishment_path(quantity=quantity)
    path = tmp_path / "replenishment-composition.sqlite3"
    store = SQLitePersistence(path)
    fixtures = _replenishment_fixtures(store, quantity=quantity)
    origin = p2p.ORIGIN
    correlation_id = deterministic_id(
        "trading-company-replenishment",
        fixtures.warehouse.origin_stock_id,
        fixtures.procurement.purchase_order_id,
    )

    messages = []
    effects = []

    message = replenishment._publish(
        BoundaryService(store),
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
    store, _, consumption = _claim_after_worker_death(
        store,
        path,
        registry_factory=lambda: replenishment._registry(fixtures),
        owner_id="p2p-worker",
        now=message.produced_at,
    )
    effects.append(consumption.consumer_effect_id)
    intent = store.command(consumption.consumer_effect_id)
    assert intent is not None
    replenishment._execute_procurement(
        store,
        intent=intent,
        fixtures=fixtures,
        quantity=quantity,
        correlation_id=correlation_id,
    )
    store = _reopen(store, path)

    message = replenishment._publish(
        BoundaryService(store),
        contract_name="p2p.inventory_receipt_ready",
        source_domain="procure_to_pay",
        source_identity=fixtures.procurement.receipt_id,
        destination_domain="warehouse_management",
        occurrence_key="inventory-receipt-ready",
        correlation_id=correlation_id,
        causation_id=messages[-1].message_id,
        produced_at=replenishment._next_time(store, messages[-1].produced_at),
        payload={
            "receipt_id": fixtures.procurement.receipt_id,
            "stock_id": fixtures.warehouse.origin_stock_id,
            "sku": p2p.SKU,
            "quantity": quantity,
        },
    )
    messages.append(message)
    store, receipt_lease, consumption = _claim_after_worker_death(
        store,
        path,
        registry_factory=lambda: replenishment._registry(fixtures),
        owner_id="warehouse-worker",
        now=message.produced_at,
    )
    effects.append(consumption.consumer_effect_id)
    intent = store.command(consumption.consumer_effect_id)
    assert intent is not None
    replenishment._execute_warehouse_receipt(
        store,
        intent=intent,
        fixtures=fixtures,
        quantity=quantity,
        correlation_id=correlation_id,
    )
    store = _reopen(store, path)

    replay = BoundaryService(store).consume(
        lease=receipt_lease,
        registry=replenishment._registry(fixtures),
        now=message.produced_at + timedelta(minutes=1),
    )
    assert replay == consumption

    replenishment._release_p2p_staging(
        store,
        caused_by=intent,
        fixtures=fixtures,
        quantity=quantity,
        correlation_id=correlation_id,
    )
    store = _reopen(store, path)

    message = replenishment._publish(
        BoundaryService(store),
        contract_name="accounting.entry_requested",
        source_domain="procure_to_pay",
        source_identity=fixtures.procurement.receipt_id,
        destination_domain="record_to_report",
        occurrence_key="replenishment-accounting-entry",
        correlation_id=correlation_id,
        causation_id=messages[-1].message_id,
        produced_at=replenishment._next_time(store, messages[-1].produced_at),
        payload={
            "receipt_id": fixtures.procurement.receipt_id,
            "journal_id": fixtures.accounting.journal_id,
            "amount": 500.0,
            "currency": "USD",
        },
    )
    messages.append(message)
    store, _, consumption = _claim_after_worker_death(
        store,
        path,
        registry_factory=lambda: replenishment._registry(fixtures),
        owner_id="r2r-worker",
        now=message.produced_at,
    )
    effects.append(consumption.consumer_effect_id)
    intent = store.command(consumption.consumer_effect_id)
    assert intent is not None
    replenishment._execute_accounting(
        store,
        intent=intent,
        fixtures=fixtures,
        correlation_id=correlation_id,
    )
    store = _reopen(store, path)

    assert tuple(message.message_id for message in messages) == continuous.message_ids
    assert tuple(effects) == continuous.effect_ids
    assert _replenishment_business_state(store, fixtures) == (
        continuous.persistence.entity(
            "warehouse_management_stock",
            continuous.stock_id,
        ),
        continuous.persistence.entity("purchase_order", continuous.purchase_order_id),
        continuous.persistence.entity("receipt", continuous.receipt_id),
        continuous.persistence.entity("journal_entry", continuous.journal_id),
        tuple(continuous.persistence.container_states()),
    )
    assert _boundary_semantics(store) == _boundary_semantics(continuous.persistence)

    with store.transaction() as uow:
        assert all(
            delivery.attempts == 2 and delivery.claim_epoch == 2
            for delivery in uow.boundary_deliveries()
        )
    store.close()


def test_composed_consumer_fault_rolls_back_intent_before_retry(tmp_path) -> None:
    path = tmp_path / "composition-handler-fault.sqlite3"
    store = SQLitePersistence(path)
    fixtures = _customer_fixtures(store)
    correlation_id = deterministic_id(
        "trading-company-customer-demand",
        fixtures.o2c.order_id,
    )
    message = customer._publish(
        BoundaryService(store),
        contract_name="o2c.fulfillment_requested",
        source_domain="order_to_cash",
        source_identity=fixtures.o2c.order_id,
        destination_domain="warehouse_fulfillment",
        occurrence_key="fulfillment-requested",
        correlation_id=correlation_id,
        causation_id=None,
        produced_at=o2c.ORIGIN,
        payload={
            "order_id": fixtures.o2c.order_id,
            "fulfillment_order_id": fixtures.fulfillment.order_id,
            "requested_quantity": 10.0,
            "sku": fulfillment.PRIMARY_SKU,
        },
    )

    service = BoundaryService(store)
    lease = service.claim_next(
        owner_id="failing-worker",
        now=message.produced_at,
        lease_duration=timedelta(seconds=5),
    )
    assert lease is not None

    real_registry = customer._registry(fixtures)
    real_handler = real_registry.resolve(message)
    failing_registry = BoundaryConsumerRegistry()
    failed_effect_ids: list[str] = []

    def fail_after_intent(boundary_message, uow):
        effect_id = real_handler(boundary_message, uow)
        failed_effect_ids.append(effect_id)
        assert uow.get_command(effect_id) is not None
        raise RuntimeError("injected consumer failure before acknowledgement")

    failing_registry.register(
        destination_domain=message.destination_domain,
        contract_name=message.contract_name,
        contract_version=message.contract_version,
        handler=fail_after_intent,
    )

    with pytest.raises(RuntimeError, match="injected consumer failure"):
        service.consume(
            lease=lease,
            registry=failing_registry,
            now=message.produced_at + timedelta(seconds=1),
        )

    assert len(failed_effect_ids) == 1
    failed_effect_id = failed_effect_ids[0]
    assert store.command(failed_effect_id) is None
    assert service.consumption(lease.delivery_id) is None
    current = service.delivery(lease.delivery_id)
    assert current is not None and current.status is DeliveryStatus.CLAIMED

    store = _reopen(store, path)
    assert store.command(failed_effect_id) is None
    service = BoundaryService(store)
    reclaimed = service.claim_next(
        owner_id="recovery-worker",
        now=message.produced_at + timedelta(seconds=6),
        lease_duration=timedelta(hours=1),
    )
    assert reclaimed is not None and reclaimed.epoch == lease.epoch + 1
    consumption = service.consume(
        lease=reclaimed,
        registry=customer._registry(fixtures),
        now=message.produced_at + timedelta(seconds=7),
    )
    assert store.command(consumption.consumer_effect_id) is not None
    store.close()

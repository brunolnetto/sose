"""A worker crash cannot strand picked WF inventory consumption or shipped dispatch."""
from __future__ import annotations

from datetime import timedelta

import pytest

from sose.composition import trading_company_customer as customer
from sose.composition.boundary import BoundaryConsumerRegistry, BoundaryService
from sose.examples.warehouse_fulfillment import simulation as fulfillment
from sose.persistence.sqlite import SQLitePersistence


@pytest.mark.parametrize(
    "crash_contract",
    ["warehouse.inventory_consumption_requested", "warehouse.dispatch_ready"],
)
def test_reconcile_shipped_egress_after_worker_death_without_python_stage_list(
    monkeypatch, tmp_path, crash_contract,
):
    database = tmp_path / "trading-company-crash.sqlite3"
    held = {}

    def persistent_store():
        store = SQLitePersistence(database)
        held["store"] = store
        return store

    original_publish = customer._publish

    def crash_after_shipment(service, *, contract_name, **kwargs):
        if contract_name == crash_contract:
            raise RuntimeError("injected worker death after durable shipment")
        return original_publish(service, contract_name=contract_name, **kwargs)

    monkeypatch.setattr(customer, "MemoryPersistence", persistent_store)
    monkeypatch.setattr(customer, "_publish", crash_after_shipment)
    with pytest.raises(RuntimeError, match="injected worker death"):
        customer.run_customer_demand_path()
    held["store"].close()
    monkeypatch.setattr(customer, "_publish", original_publish)

    recovered = SQLitePersistence(database)
    with recovered.transaction() as uow:
        messages = [
            uow.get_boundary_message(delivery.message_id)
            for delivery in uow.boundary_deliveries()
        ]
    reserved = next(
        m for m in messages if m is not None
        and m.contract_key == "warehouse.inventory_reserved.v1"
    )
    info = reserved.payload()
    shipped = recovered.entity("warehouse_fulfillment_order", info["fulfillment_order_id"])
    assert shipped is not None
    assert shipped.state == (
        "picking" if crash_contract == "warehouse.inventory_consumption_requested"
        else "shipped"
    )

    # Bounded reconciliation must not publish a single message when the
    # remaining scheduler budget is exhausted, even after a crash.
    with recovered.transaction() as uow:
        before_messages = {d.message_id for d in uow.boundary_deliveries()}
    customer.reconcile_shipped_fulfillment_egress(
        recovered, correlation_id=reserved.correlation_id,
        max_new_messages=0,
    )
    with recovered.transaction() as uow:
        assert {d.message_id for d in uow.boundary_deliveries()} == before_messages
    with pytest.raises(ValueError, match="max_new_messages"):
        customer.reconcile_shipped_fulfillment_egress(
            recovered, correlation_id=reserved.correlation_id,
            max_new_messages=-1,
        )

    # First reconstruct from authoritative state, then consume through the
    # real BoundaryService with durable fencing/consumer-effect identity.
    outgoing = customer.reconcile_shipped_fulfillment_egress(
        recovered, correlation_id=reserved.correlation_id,
        max_new_messages=1,
    )
    assert outgoing[0].contract_key == "warehouse.inventory_consumption_requested.v1"
    assert outgoing[0].causation_id == reserved.message_id
    assert outgoing[0].payload()["reservation_reference"] == info["reservation_reference"]
    assert outgoing[0].payload()["stock_id"] == info["stock_id"]
    consumption_info = outgoing[0].payload()
    if crash_contract == "warehouse.inventory_consumption_requested":
        assert len(outgoing) == 1
        registry = BoundaryConsumerRegistry()
        registry.register(
            destination_domain="warehouse_management",
            contract_name="warehouse.inventory_consumption_requested",
            contract_version=1,
            handler=customer._intent_handler(
                intent_name="composition.consume_fulfillment_inventory",
                entity_type="warehouse_management_stock",
                entity_id=consumption_info["stock_id"],
            ),
        )
        service = BoundaryService(recovered)

        # A published pick/WM request is NOT claimable while its accepted
        # reservation intent is still in progress after the worker crash.
        # Resuming the ancestor is necessary before the WM child may apply.
        assert service.claim_next(
            owner_id="blocked-warehouse-worker",
            now=outgoing[0].produced_at,
            lease_duration=timedelta(hours=1),
        ) is None
        with recovered.transaction() as uow:
            reservation_effect = next(
                uow.get_boundary_consumption(delivery.delivery_id).consumer_effect_id
                for delivery in uow.boundary_deliveries()
                if delivery.message_id == reserved.message_id
            )
        assert recovered.command(reservation_effect) is not None
        customer._execute_intent(
            recovered,
            effect_id=reservation_effect,
            fixtures=customer._CustomerFixtures(
                o2c=None,
                fulfillment=fulfillment.WarehouseEntities(
                    order_id=info["fulfillment_order_id"], lot_ids=(),
                ),
                warehouse=None, logistics=None, payments=None, r2r=None,
            ),
            correlation_id=reserved.correlation_id,
        )
        assert recovered.command(reservation_effect) is None
        assert recovered.entity(
            "warehouse_fulfillment_order", info["fulfillment_order_id"]
        ).state == "shipped"

        lease = service.claim_next(
            owner_id="recovered-warehouse-worker",
            now=outgoing[0].produced_at,
            lease_duration=timedelta(hours=1),
        )
        assert lease is not None
        boundary_consumption = service.consume(
            lease=lease, registry=registry, now=outgoing[0].produced_at,
        )
        assert recovered.command(boundary_consumption.consumer_effect_id) is not None
        with recovered.transaction() as uow:
            assert uow.get_boundary_consumption(lease.delivery_id) == boundary_consumption
        customer._execute_intent(
            recovered,
            effect_id=boundary_consumption.consumer_effect_id,
            fixtures=customer._CustomerFixtures(
                o2c=None, fulfillment=None, warehouse=None,
                logistics=None, payments=None, r2r=None,
            ),
            correlation_id=reserved.correlation_id,
        )
        replayed = service.consume(
            lease=lease,
            registry=registry,
            now=outgoing[0].produced_at + timedelta(minutes=1),
        )
        assert replayed.consumer_effect_id == boundary_consumption.consumer_effect_id


    # An alternate/pending reservation is not an accepted WF business fact.
    # It must not be used as a source for WM consumption after a crash.
    decoy = customer._publish(
        BoundaryService(recovered),
        contract_name="warehouse.inventory_reserved",
        source_domain="warehouse_management",
        source_identity="unaccepted-foreign-stock",
        destination_domain="warehouse_fulfillment",
        occurrence_key="unaccepted-reservation",
        correlation_id=reserved.correlation_id,
        causation_id=reserved.message_id,
        produced_at=reserved.produced_at,
        payload={
            "fulfillment_order_id": info["fulfillment_order_id"],
            "stock_id": "unaccepted-foreign-stock",
            "reservation_reference": "unaccepted-reservation",
            "sku": info["sku"],
            "quantity": info["quantity"],
        },
    )
    assert outgoing[0].causation_id != decoy.message_id
    assert customer.reconcile_shipped_fulfillment_egress(
        recovered, correlation_id=reserved.correlation_id,
    )[0].message_id == outgoing[0].message_id

    finished = customer.reconcile_shipped_fulfillment_egress(
        recovered, correlation_id=reserved.correlation_id,
    )
    assert len(finished) == 2
    assert finished[0].message_id == outgoing[0].message_id
    assert finished[1].contract_key == "warehouse.dispatch_ready.v1"
    assert finished[1].causation_id == finished[0].message_id
    assert finished[1].payload()["shipment_id"]
    again = customer.reconcile_shipped_fulfillment_egress(
        recovered, correlation_id=reserved.correlation_id,
    )
    assert [m.message_id for m in again] == [m.message_id for m in finished]
    with recovered.transaction() as uow:
        all_deliveries = tuple(uow.boundary_deliveries())
    assert len({delivery.delivery_id for delivery in all_deliveries}) == len(all_deliveries)
    recovered.close()

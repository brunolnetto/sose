"""A worker crash cannot strand shipped WF inventory consumption or dispatch."""
from __future__ import annotations

from datetime import timedelta

import pytest

from sose.composition import trading_company_customer as customer
from sose.composition.boundary import BoundaryService
from sose.examples.warehouse_management import simulation as wm
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
    assert shipped is not None and shipped.state == "shipped"

    outgoing = customer.reconcile_shipped_fulfillment_egress(
        recovered, correlation_id=reserved.correlation_id,
    )
    assert outgoing[0].contract_key == "warehouse.inventory_consumption_requested.v1"
    assert outgoing[0].causation_id == reserved.message_id
    consumption_info = outgoing[0].payload()
    if crash_contract == "warehouse.inventory_consumption_requested":
        # Recovery publishes the first message, but cannot dispatch while
        # Warehouse Management still owns an unconsumed stock reservation.
        assert len(outgoing) == 1
        _, engine = wm.build_runtime(recovered, now=outgoing[0].produced_at)
        assert wm.consume_external_reservation(
            recovered, engine,
            stock_id=consumption_info["stock_id"],
            quantity=consumption_info["quantity"],
            sku=consumption_info["sku"],
            reservation_reference=consumption_info["reservation_reference"],
            consumption_reference=consumption_info["consumption_reference"],
            correlation_id=reserved.correlation_id,
        )
        assert not wm.consume_external_reservation(
            recovered, engine,
            stock_id=consumption_info["stock_id"],
            quantity=consumption_info["quantity"],
            sku=consumption_info["sku"],
            reservation_reference=consumption_info["reservation_reference"],
            consumption_reference=consumption_info["consumption_reference"],
            correlation_id=reserved.correlation_id,
        )

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

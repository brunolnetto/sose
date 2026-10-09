"""PC6 ingress must create durable fulfillment from the received contract, not fixtures."""
from __future__ import annotations

from copy import deepcopy

import pytest

from sose.composition import trading_company_customer as customer
from sose.core.events import Command
from sose.core.identity import deterministic_id
from sose.examples.order_to_cash import simulation as o2c
from sose.persistence.memory import MemoryPersistence


def _request(persistence, source_order_id, *, changes=None):
    target_id = deterministic_id(
        "entity", "warehouse_fulfillment_order",
        "warehouse-reference", f"order:{source_order_id}",
    )
    payload = {
        "order_id": source_order_id,
        "fulfillment_order_id": target_id,
        "requested_quantity": 10.0,
        "sku": "widget-a",
    }
    payload.update(changes or {})
    return Command(
        command_id=deterministic_id("ingress-test", source_order_id),
        name="composition.request_fulfillment_inventory",
        entity_type="warehouse_fulfillment_order",
        entity_id=target_id,
        due_at=o2c.ORIGIN,
        issued_at=o2c.ORIGIN,
        payload=payload,
    )


def test_payload_driven_fulfillment_creates_exactly_one_durable_order():
    persistence = MemoryPersistence()
    source = o2c.seed_reference(persistence)
    intent = _request(persistence, source.order_id)
    assert persistence.entity("warehouse_fulfillment_order", intent.entity_id) is None

    created = customer._materialize_fulfillment_from_request(
        persistence, intent=intent, expected_sales_order_id=source.order_id,
    )
    assert created.order_id == intent.entity_id
    order = persistence.entity("warehouse_fulfillment_order", created.order_id)
    assert order is not None
    assert order.state == "requested"
    assert order.attributes["requested_quantity"] == 10.0
    assert order.attributes["requested_sku"] == "widget-a"
    assert order.attributes["inventory_owner"] == "warehouse_management"
    assert customer._materialize_fulfillment_from_request(
        persistence, intent=intent, expected_sales_order_id=source.order_id,
    ) == created
    assert len(tuple(
        entity for entity in persistence.entities()
        if entity.entity_type == "warehouse_fulfillment_order"
    )) == 1


@pytest.mark.parametrize(
    "change,match",
    [
        ({"order_id": "foreign-sales-order"}, "source sales order mismatch"),
        ({"fulfillment_order_id": "foreign-fulfillment"}, "identity mismatch"),
        ({"requested_quantity": 0}, "invalid requested_quantity"),
        ({"requested_quantity": float("inf")}, "invalid requested_quantity"),
        ({"sku": ""}, "invalid sku"),
    ],
)
def test_payload_ingress_rejects_invalid_request_before_durable_creation(change, match):
    persistence = MemoryPersistence()
    source = o2c.seed_reference(persistence)
    intent = _request(persistence, source.order_id, changes=change)
    with pytest.raises(ValueError, match=match):
        customer._materialize_fulfillment_from_request(
            persistence, intent=intent, expected_sales_order_id=source.order_id,
        )
    assert not any(
        entity.entity_type == "warehouse_fulfillment_order"
        for entity in persistence.entities()
    )


def test_conflicting_replay_cannot_change_persisted_warehouse_order():
    persistence = MemoryPersistence()
    source = o2c.seed_reference(persistence)
    intent = _request(persistence, source.order_id)
    customer._materialize_fulfillment_from_request(
        persistence, intent=intent, expected_sales_order_id=source.order_id,
    )
    conflict = _request(persistence, source.order_id, changes={"requested_quantity": 12.0})
    with pytest.raises(ValueError, match="conflicts with durable order"):
        customer._materialize_fulfillment_from_request(
            persistence, intent=conflict, expected_sales_order_id=source.order_id,
        )
    original = persistence.entity("warehouse_fulfillment_order", intent.entity_id)
    assert original.attributes["requested_quantity"] == 10.0


def test_customer_path_does_not_preseed_fulfillment_before_ingress(monkeypatch):
    original = customer.fulfillment.seed_composed_reference
    materializations = []

    def observed(persistence, **kwargs):
        request_key = kwargs["request_key"]
        order_id = deterministic_id(
            "entity", "warehouse_fulfillment_order", "warehouse-reference", request_key,
        )
        assert persistence.entity("warehouse_fulfillment_order", order_id) is None
        materializations.append(request_key)
        return original(persistence, **kwargs)

    monkeypatch.setattr(customer.fulfillment, "seed_composed_reference", observed)
    result = customer.run_customer_demand_path()
    assert materializations == [f"order:{result.o2c_order_id}"]
    assert persistence_order := result.persistence.entity(
        "warehouse_fulfillment_order", result.fulfillment_order_id
    )
    assert persistence_order.state == "shipped"
